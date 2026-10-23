"""Atomic domain mutations shared by API and worker."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from . import db


class Conflict(ValueError):
    pass


def body_hash(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def enqueue_incidents(session, row, state):
    from .bridge import build_envelope, should_dispatch
    connection = session.get(db.Connection, '911')
    supports_location = bool(connection and connection.status == 'ok'
        and connection.details.get('supports_location') is True)
    identifiers = [i['id'] for i in state.get('incidents', [])]
    existing = set(session.scalars(select(db.Outbox.id).where(db.Outbox.id.in_(identifiers))).all()) if identifiers else set()
    for incident in state.get('incidents', []):
        if incident['id'] in existing or not should_dispatch(incident):
            continue
        body = build_envelope(incident, supports_location=True) if supports_location else build_envelope(incident)
        session.add(db.Outbox(id=incident['id'], run_id=row.run_id, payload=body,
            body_hash=body_hash(body), status='pending'))
        incident['dispatch_status'] = 'pending'


def find_incident_state(session, current_row, incident_id):
    if any(i['id'] == incident_id for i in current_row.payload.get('incidents', [])):
        return current_row, current_row.payload, current_row.run_id
    job = session.get(db.Outbox, incident_id)
    if job:
        archived = session.get(db.ArchivedRun, job.run_id)
        if archived and any(i['id'] == incident_id for i in archived.payload.get('incidents', [])):
            return archived, archived.payload, archived.id
    # Local-only incidents have no outbox; archived runs are bounded and queried only on feedback.
    for archived in session.scalars(select(db.ArchivedRun).order_by(db.ArchivedRun.archived_at.desc())):
        if any(i['id'] == incident_id for i in archived.payload.get('incidents', [])):
            return archived, archived.payload, archived.id
    raise LookupError('Unknown transit incident. Use its transit incident_id, not a remote 911 identifier.')


def wake_feedback(session, incident_id):
    """Fresh callbacks reopen reconciliation, including previously retired work."""
    poll = session.get(db.FeedbackPoll, incident_id, with_for_update=True)
    if poll is None:
        # Serialize with lazy worker bootstrap so a callback arriving before the
        # first scheduled poll is still treated as a fresh, explicit wake-up.
        job = session.get(db.Outbox, incident_id, with_for_update=True)
        if job is None or job.status != 'sent' or not job.remote_incident_id:
            return
        poll = session.scalar(select(db.FeedbackPoll).where(db.FeedbackPoll.outbox_id == incident_id)
            .with_for_update().execution_options(populate_existing=True))
        if poll is None:
            poll = db.FeedbackPoll(outbox_id=incident_id, details={})
            session.add(poll)
    poll.state = 'active'
    poll.next_poll_at = db.now()
    poll.terminal_since = None
    poll.failures = 0
    poll.last_error = None
    poll.updated_at = db.now()
    # Keep an in-flight lease. Its completion must notice the newer callback.
    poll.details = {**(poll.details or {}),
        'wake_generation': (poll.details or {}).get('wake_generation', 0) + 1}


def record_report(payload, *, reactivate_poll=True):
    from .engine import apply_feedback
    digest = body_hash(payload)
    with db.state_transaction() as (session, current, current_state):
        existing = session.scalar(select(db.Report).where(db.Report.source == payload['source'],
            db.Report.external_id == payload['external_id']))
        if existing:
            if existing.body_hash != digest:
                raise Conflict('This source/external_id was already used with a different report.')
            return {'id': existing.id, 'duplicate': True, 'incident_id': existing.incident_id}
        incident_id = payload.get('incident_id')
        run_id = None
        if incident_id:
            owner, state, run_id = find_incident_state(session, current, incident_id)
            feedback = dict(payload)
            # One completed hospital journey does not close a multi-agency incident.
            clinical = any(word in feedback['source'].lower() for word in ('hospital', 'clinic'))
            if feedback.get('status') == 'resolved' and feedback['source'] != '911' and not clinical:
                feedback['status'] = 'transporting'
            apply_feedback(state, feedback)
            flag_modified(owner, 'payload')
            if reactivate_poll:
                wake_feedback(session, incident_id)
        ident = str(uuid.uuid4())
        instant = datetime.fromtimestamp(current_state['t'], timezone.utc).isoformat()
        session.add(db.Report(id=ident, source=payload['source'], external_id=payload['external_id'],
            incident_id=incident_id, run_id=run_id, body_hash=digest, payload=payload,
            transit_sim_time=instant))
        return {'id': ident, 'duplicate': False, 'incident_id': incident_id}


def update_dispatch(incident_id, status, remote_id=None):
    with db.state_transaction() as (session, current, state):
        try:
            owner, payload, _ = find_incident_state(session, current, incident_id)
        except LookupError:
            return
        for incident in payload['incidents']:
            if incident['id'] == incident_id:
                incident['dispatch_status'] = status
                if remote_id:
                    incident['remote_incident_id'] = remote_id
                flag_modified(owner, 'payload')
                break
