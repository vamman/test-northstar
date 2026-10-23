"""One clock worker and durable at-least-once bridge; network I/O never holds the clock lock."""
from __future__ import annotations

import asyncio
import logging
from app.environment_client import snapshot as environment_snapshot
import os
import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy import or_, select

from . import db, engine, services, workplaces, polling
from .bridge import TIMEOUT, BridgeError, connection_status, observe_incident, send_envelope

log = logging.getLogger('northstar.transit')


def tick(seconds):
    observation = environment_snapshot('transit')
    with db.state_transaction() as (session, row, state):
        from app import northstar_clock
        delta = northstar_clock.seconds(state['t'], state) if northstar_clock.enabled() else seconds * state.get('speed', 60)
        if not state.get('paused') and delta:
            engine.advance(state, delta, registry=workplaces.registry(session,state), environment=observation)
        services.enqueue_incidents(session, row, state)
        row.worker_at = db.now()


def claim_dispatch():
    with db.Session.begin() as session:
        job = session.scalar(select(db.Outbox).where(db.Outbox.status.in_(['pending', 'retry']),
            db.Outbox.next_attempt_at <= db.now(),
            or_(db.Outbox.lease_until.is_(None), db.Outbox.lease_until < db.now()))
            .order_by(db.Outbox.created_at).with_for_update(skip_locked=True).limit(1))
        if job is None:
            return None
        job.attempts += 1
        job.lease_until = db.now() + timedelta(seconds=90)
        return {'id': job.id, 'payload': job.payload, 'attempts': job.attempts}


async def dispatch_once(client=None):
    job = await asyncio.to_thread(claim_dispatch)
    if not job:
        return False
    try:
        receipt = await send_envelope(job['payload'], **({'client': client} if client is not None else {}))
    except BridgeError as exc:
        status = 'retry' if exc.retryable else 'blocked'
        with db.Session.begin() as session:
            row = session.get(db.Outbox, job['id'], with_for_update=True)
            row.status = status
            row.error = str(exc)[:500]
            row.lease_until = None
            row.next_attempt_at = db.now() + timedelta(seconds=min(300, 2 ** min(row.attempts, 8)))
        await asyncio.to_thread(services.update_dispatch, job['id'], status)
    else:
        remote_id = receipt.get('remote_incident_id') or receipt.get('call', {}).get('incident_id')
        with db.Session.begin() as session:
            row = session.get(db.Outbox, job['id'], with_for_update=True)
            row.status = 'sent'
            row.receipt = receipt
            row.remote_incident_id = str(remote_id) if remote_id else None
            row.sent_at = db.now()
            row.error = None
            row.lease_until = None
        await asyncio.to_thread(services.update_dispatch, job['id'], 'sent', remote_id)
    return True


# Public aliases keep scheduler diagnostics and focused tests independent of I/O.
claim_feedback = polling.claim_feedback
complete_feedback = polling.complete_feedback


async def _renew_observation_lease(claim):
    while True:
        await asyncio.sleep(30)
        if not await asyncio.to_thread(polling.renew_feedback, claim):
            return


async def _observe_claim(claim, client=None):
    if not await asyncio.to_thread(polling.renew_feedback, claim):
        return
    renewal = asyncio.create_task(_renew_observation_lease(claim))
    reports = None
    error = None
    try:
        kwargs = {'force_refresh': True} if claim.get('force_refresh') else {}
        if client is not None:
            kwargs['client'] = client
        reports = await asyncio.wait_for(
            observe_incident(claim['remote_incident_id'], claim['id'], **kwargs), timeout=90)
        if not await asyncio.to_thread(polling.renew_feedback, claim):
            return
        for report in reports:
            report = {'schema_version': '1.0', 'synthetic': True, 'kind': 'incident_status', **report}
            await asyncio.to_thread(services.record_report, report, reactivate_poll=False)
    except asyncio.TimeoutError:
        error = 'Observation exceeded the 90-second attempt deadline'
        log.warning('Feedback observation timed out for %s', claim['id'])
    except (BridgeError, services.Conflict, LookupError) as exc:
        error = str(exc)
        log.warning('Feedback observation unavailable for %s: %s', claim['id'], error[:200])
    except Exception as exc:
        # Unknown implementation/transport failures retain durable work with the
        # same bounded backoff rather than retrying every five seconds.
        error = f'{type(exc).__name__}: observation failed'
        log.exception('Unexpected feedback observation failure for %s', claim['id'])
    finally:
        renewal.cancel()
        try:
            await renewal
        except asyncio.CancelledError:
            pass
        except Exception:
            log.warning('Feedback lease renewal failed for %s; completing with the original token', claim['id'], exc_info=True)
    await asyncio.to_thread(complete_feedback, claim, reports=reports, error=error)


async def feedback_once(client=None):
    claims = await asyncio.to_thread(claim_feedback)
    # A bounded set runs concurrently so queued claims do not lose their leases
    # while another incident waits for a remote simulator.
    if claims:
        await asyncio.gather(*(_observe_claim(claim, client) for claim in claims))
    return len(claims)


async def connections_once(client=None):
    rows = await connection_status(**({'client': client} if client is not None else {}))
    with db.Session.begin() as session:
        for item in rows:
            ident = item.get('id') or item.get('source')
            if not ident:
                continue
            row = session.get(db.Connection, ident)
            if not row:
                row = db.Connection(id=ident, status='unknown')
                session.add(row)
            row.status = item.get('status', 'unknown')
            row.details = {k: v for k, v in item.items() if k not in ('id', 'source', 'status')}
            actual = item.get('observed_at') or item.get('health_observed_at')
            if isinstance(actual, str):
                try:
                    row.observed_at = datetime.fromisoformat(actual.replace('Z', '+00:00'))
                except ValueError:
                    row.observed_at = db.now()
            else:
                row.observed_at = db.now()


async def clock_loop():
    interval = max(.5, float(os.getenv('TICK_SECONDS', '1')))
    while True:
        started = time.monotonic()
        try:
            await asyncio.to_thread(tick, interval)
        except Exception:
            log.exception('Clock transaction failed; retained last committed state')
        await asyncio.sleep(max(.05, interval - (time.monotonic() - started)))


async def _dispatch_loop(client):
    while True:
        started = time.monotonic()
        try:
            for _ in range(5):
                if not await dispatch_once(client):
                    break
        except Exception:
            log.exception('Dispatch cycle failed; durable work retained for the next cycle')
        await asyncio.sleep(max(.1, 5 - (time.monotonic() - started)))


async def _feedback_loop(client, health_ready):
    # Warm the shared health cache once before the first incident batch. Later
    # endpoint refreshes run independently and cannot block the dispatch loop.
    await health_ready.wait()
    while True:
        started = time.monotonic()
        try:
            await feedback_once(client)
        except Exception:
            log.exception('Feedback cycle failed; durable schedules retained')
        await asyncio.sleep(max(.1, 5 - (time.monotonic() - started)))


async def _connections_loop(client, health_ready):
    while True:
        started = time.monotonic()
        try:
            await connections_once(client)
        except Exception:
            log.exception('Connection snapshot failed; previous observations retained')
        finally:
            health_ready.set()
        await asyncio.sleep(max(.1, 30 - (time.monotonic() - started)))


async def bridge_loop():
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, trust_env=False) as client:
        health_ready = asyncio.Event()
        await asyncio.gather(_dispatch_loop(client), _feedback_loop(client, health_ready),
                             _connections_loop(client, health_ready))


async def run():
    db.init_db()
    await asyncio.gather(clock_loop(), bridge_loop())


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(run())
