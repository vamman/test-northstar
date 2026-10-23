"""Durable bounded reconciliation schedules, separate from immutable dispatch jobs.

All scheduling uses wall time. Retiring a poll never resolves an incident: only
validated source reports do that. Database claims are short; network I/O happens
outside their locks, and leases/generations reject stale completions.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

from sqlalchemy import exists, func, or_, select

from . import db

ACTIVE_SECONDS=30
PAUSED_SECONDS=120
GRACE_INTERVAL_SECONDS=120
GRACE_WINDOW_SECONDS=900
MAX_BACKOFF_SECONDS=900
MAX_JOBS=4
LEASE_SECONDS=120
BOOTSTRAP_BATCH=8
FRESH_WINDOW_SECONDS=3600


def policy():
    return {'active_interval_seconds':ACTIVE_SECONDS,'paused_interval_seconds':PAUSED_SECONDS,
            'grace_interval_seconds':GRACE_INTERVAL_SECONDS,'grace_window_seconds':GRACE_WINDOW_SECONDS,
            'max_jobs_per_cycle':MAX_JOBS,'max_backoff_seconds':MAX_BACKOFF_SECONDS,
            'lease_seconds':LEASE_SECONDS,'bootstrap_batch':BOOTSTRAP_BATCH,
            'connection_interval_seconds':30}


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _now(value=None):return _utc(value or db.now())


@contextmanager
def _transaction():
    with db.Session.begin() as session:
        if db.engine.dialect.name=='sqlite':session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        yield session


def observation_evidence(reports):
    """Positive terminal evidence from a complete command observation plus EMS reports.

    Callback status alone is insufficient. Existing pre-metadata bridge reports
    remain usable only when their validated assignment list and EMS reports exist.
    """
    rows=reports if isinstance(reports,list) else []
    commands=[r for r in rows if isinstance(r,dict) and r.get('source')=='911'
              and isinstance(r.get('details'),dict) and r['details'].get('scope')=='command_incident']
    command=commands[0] if commands else {}
    details=command.get('details',{})
    assignments=details.get('assignments')
    known=isinstance(assignments,list) and all(isinstance(a,dict) and all(
        isinstance(a.get(key),str) and a[key].strip() for key in ('assignment_id','agency','status'))
        for a in assignments)
    if details.get('assignments_known') is False:known=False
    assignments=assignments if known else []
    errors=list(details.get('observation_errors') or details.get('hospital_observation_errors') or [])
    if details.get('hospital_health_unavailable') and not errors:errors.append({'error':'Hospital health unavailable'})
    if not command:errors.append({'error':'Missing validated command observation'})
    elif not known:errors.append({'error':'Command observation omitted complete assignment records'})
    linked={str(a['remote_id']) for a in assignments if a.get('agency')=='ems' and a.get('remote_id')}
    missing_ems_identity=any(a.get('agency')=='ems' and not a.get('remote_id') for a in assignments)
    hospitals={}
    for report in rows:
        if not isinstance(report,dict) or report.get('source')!='hospital':continue
        info=report.get('details')
        if not isinstance(info,dict) or info.get('scope')!='ems_journey' or not info.get('remote_incident_id'):continue
        hospitals[str(info['remote_incident_id'])]=info
    completed={ident for ident,info in hospitals.items() if info.get('remote_status')=='completed' and ident in linked}
    # Assignment-side polling errors are still observation failures, even when
    # another independently cached EMS response is available.
    if any(a.get('last_poll_error') for a in assignments):errors.append({'error':'911 assignment observation has an upstream polling error'})
    terminal=command.get('status')=='resolved' and known and not missing_ems_identity and linked<=completed and not errors
    assignment_fields=('assignment_id','agency','status','remote_status','remote_id','external_id','hospital_patient_id','hospital_encounter_id')
    semantic={'command_status':details.get('command_status') or details.get('remote_status') or command.get('status'),
              'assignments_known':known,'assignments':sorted(({key:a.get(key) for key in assignment_fields} for a in assignments),key=lambda a:str(a.get('assignment_id'))),
              'ems':{ident:{key:info.get(key) for key in ('remote_status','patient_id','encounter_id','completed_at')}
                     for ident,info in sorted(hospitals.items()) if ident in linked}}
    fingerprint=hashlib.sha256(json.dumps(semantic,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'terminal':terminal,'fingerprint':fingerprint,'command_status':command.get('status'),
            'assignments_known':known,'linked_ems_ids':sorted(linked),'completed_ems_ids':sorted(completed),
            'pending_ems_count':len(linked-completed)+int(missing_ems_identity),
            'paused':details.get('command_paused') is True or details.get('hospital_paused') is True,
            'errors':errors}


def _historical_evidence(session,job):
    records=list(session.scalars(select(db.Report).where(db.Report.incident_id==job.id)
                                .order_by(db.Report.observed_at.desc()).limit(128)))
    # Keep the most recently stored report for each independent source identity.
    command=None;hospitals={};used=[]
    for record in records:
        report=record.payload or {};details=report.get('details') or {}
        if report.get('source')=='911' and details.get('scope')=='command_incident' and command is None:
            command=report;used.append(record)
        elif report.get('source')=='hospital' and details.get('scope')=='ems_journey':
            ident=details.get('remote_incident_id')
            if ident and ident not in hospitals:hospitals[ident]=report;used.append(record)
    reports=([command] if command else [])+list(hospitals.values())
    evidence=observation_evidence(reports)
    since=max((_utc(row.observed_at) for row in used),default=None)
    return evidence,since


def _bootstrap(session,now,limit=BOOTSTRAP_BATCH):
    if limit<=0:return 0
    missing=~exists(select(db.FeedbackPoll.outbox_id).where(db.FeedbackPoll.outbox_id==db.Outbox.id))
    eligible=select(db.Outbox).where(db.Outbox.status=='sent',db.Outbox.remote_incident_id.is_not(None),missing)
    newest=max(1,limit-2)
    selected=list(session.scalars(eligible.order_by(db.Outbox.created_at.desc())
                    .with_for_update(of=db.Outbox,skip_locked=True).limit(newest)))
    ids=[j.id for j in selected]
    remaining=limit-len(selected)
    if remaining:
        old=eligible.where(db.Outbox.id.not_in(ids)) if ids else eligible
        selected+=list(session.scalars(old.order_by(db.Outbox.created_at.asc())
                       .with_for_update(of=db.Outbox,skip_locked=True).limit(remaining)))
    for job in selected:
        evidence,since=_historical_evidence(session,job)
        terminal=evidence['terminal'] and since is not None
        details={'wake_generation':0,'last_observed_wake_generation':0,'interval_seconds':0,
                 'bootstrapped_at':now.isoformat(),'bootstrap_from_history':terminal,
                 'fingerprint':evidence['fingerprint'] if terminal else None,
                 'terminal_evidence':evidence if terminal else None}
        session.add(db.FeedbackPoll(outbox_id=job.id,state='grace' if terminal else 'active',
                    next_poll_at=now,terminal_since=min(now,since) if terminal else None,
                    failures=0,details=details,updated_at=now))
    session.flush()
    return len(selected)


def claim_feedback(now=None,limit=MAX_JOBS):
    """Claim at most four due jobs; reserve capacity for both fresh and aged work."""
    now=_now(now);limit=max(0,min(MAX_JOBS,int(limit)))
    if not limit:return []
    with _transaction() as session:
        _bootstrap(session,now)
        eligible=(select(db.FeedbackPoll,db.Outbox).join(db.Outbox,db.Outbox.id==db.FeedbackPoll.outbox_id)
            .where(db.Outbox.status=='sent',db.Outbox.remote_incident_id.is_not(None),
                   db.FeedbackPoll.state!='retired',db.FeedbackPoll.next_poll_at<=now,
                   or_(db.FeedbackPoll.lease_until.is_(None),db.FeedbackPoll.lease_until<=now)))
        cutoff=now-timedelta(seconds=FRESH_WINDOW_SECONDS)
        sent=func.coalesce(db.Outbox.sent_at,db.Outbox.created_at)
        selected=[];ids=[]
        def take(query,count):
            if count<=0:return
            if ids:query=query.where(db.FeedbackPoll.outbox_id.not_in(ids))
            rows=list(session.execute(query.with_for_update(of=db.FeedbackPoll,skip_locked=True).limit(count)))
            selected.extend(rows);ids.extend(row[0].outbox_id for row in rows)
        take(eligible.where(sent>=cutoff).order_by(db.FeedbackPoll.next_poll_at,db.Outbox.created_at.desc()),max(1,limit-1))
        take(eligible.where(sent<cutoff).order_by(db.FeedbackPoll.next_poll_at,db.Outbox.created_at),min(1,limit-len(selected)))
        take(eligible.order_by(db.FeedbackPoll.next_poll_at,db.Outbox.created_at),limit-len(selected))
        claims=[]
        for poll,job in selected:
            details=dict(poll.details or {});token=uuid4().hex
            details['lease_token']=token;details['last_claimed_at']=now.isoformat()
            poll.details=details;poll.lease_until=now+timedelta(seconds=LEASE_SECONDS);poll.updated_at=now
            generation=details.get('wake_generation',0)
            claims.append({'id':job.id,'remote_incident_id':job.remote_incident_id,'lease_token':token,
                           'wake_generation':generation,
                           'force_refresh':generation!=details.get('last_observed_wake_generation',0)})
        return claims


def renew_feedback(claim,now=None):
    now=_now(now)
    with _transaction() as session:
        poll=session.get(db.FeedbackPoll,claim['id'],with_for_update=True)
        if poll is None or (poll.details or {}).get('lease_token')!=claim['lease_token']:return False
        poll.lease_until=now+timedelta(seconds=LEASE_SECONDS)
        return True


def complete_feedback(claim,reports=None,error=None,now=None):
    """Persist next due time; reject stale workers and preserve racing callback wakes."""
    now=_now(now);evidence=observation_evidence(reports) if error is None else None
    with _transaction() as session:
        # Bootstrap and first callbacks lock Outbox before FeedbackPoll. Keep
        # the same order when updating both records to avoid a lock inversion.
        job=session.get(db.Outbox,claim['id'],with_for_update=True)
        poll=session.get(db.FeedbackPoll,claim['id'],with_for_update=True)
        if poll is None or (poll.details or {}).get('lease_token')!=claim['lease_token']:return False
        details=dict(poll.details or {});details.pop('lease_token',None)
        poll.lease_until=None;poll.updated_at=now
        if job:job.observed_at=now
        details['last_attempt_at']=now.isoformat()
        if details.get('wake_generation',0)!=claim.get('wake_generation',0):
            poll.state='active';poll.next_poll_at=now;poll.terminal_since=None;poll.failures=0;poll.last_error=None
            details['interval_seconds']=0;poll.details=details
            return True
        if evidence and evidence['errors']:
            error='; '.join(str(item.get('error','Incomplete upstream observation')) if isinstance(item,dict) else str(item) for item in evidence['errors'])
        if error is not None:
            poll.failures=(poll.failures or 0)+1
            interval=min(MAX_BACKOFF_SECONDS,ACTIVE_SECONDS*2**min(poll.failures-1,5))
            poll.state='backoff';poll.terminal_since=None;poll.last_error=str(error)[:500]
            details['interval_seconds']=interval;details['last_observation_complete']=False
        else:
            previous=details.get('fingerprint');same=previous==evidence['fingerprint']
            poll.failures=0;poll.last_error=None
            details.update(last_success_at=now.isoformat(),last_observed_wake_generation=claim.get('wake_generation',0),
                           fingerprint=evidence['fingerprint'],last_observation_complete=True,
                           command_status=evidence['command_status'],assignments_known=evidence['assignments_known'],
                           linked_ems_ids=evidence['linked_ems_ids'],completed_ems_ids=evidence['completed_ems_ids'],
                           pending_ems_count=evidence['pending_ems_count'],paused=evidence['paused'])
            if evidence['terminal']:
                if poll.terminal_since is None or not same:poll.terminal_since=now
                elapsed=max(0.,(now-_utc(poll.terminal_since)).total_seconds())
                details['terminal_evidence']=evidence
                if elapsed>=GRACE_WINDOW_SECONDS:
                    poll.state='retired';interval=0;details['retired_at']=now.isoformat()
                else:
                    poll.state='grace';interval=min(GRACE_INTERVAL_SECONDS,GRACE_WINDOW_SECONDS-elapsed)
            else:
                poll.state='active';poll.terminal_since=None;details.pop('terminal_evidence',None)
                unchanged=(details.get('unchanged_observations',0)+1) if same else 0
                details['unchanged_observations']=min(3,unchanged)
                interval=PAUSED_SECONDS if evidence['paused'] else min(PAUSED_SECONDS,ACTIVE_SECONDS*(1+unchanged))
            details['interval_seconds']=interval
        poll.next_poll_at=now+timedelta(seconds=interval)
        poll.details=details
        return True
