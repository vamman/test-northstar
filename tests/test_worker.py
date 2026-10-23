"""Durable worker transitions with mocked transport and a disposable database."""
import asyncio
import copy
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

_DATABASE_DIR = None
if "app.db" not in sys.modules:
    _DATABASE_DIR = tempfile.TemporaryDirectory(prefix="northstar-transit-worker-tests-")
    os.environ["DATABASE_URL"] = "sqlite:///" + str(Path(_DATABASE_DIR.name) / "worker.sqlite")

import pytest
from sqlalchemy import func, select

from app import db, engine, services, worker
from app.bridge import BridgeError

REMOTE_ID = "11111111-1111-4111-8111-111111111111"
CALL_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch):
    # pytest may have already imported test_api, which owns another disposable
    # SQLite engine. Refuse all databases outside the dedicated test directories.
    path = Path(db.engine.url.database)
    assert db.engine.dialect.name == "sqlite" and path.parent.name.startswith("northstar-transit-") and "tests-" in path.parent.name
    monkeypatch.setenv("SIMULATION_SEED", "42")
    monkeypatch.setenv("SIMULATION_START", "2026-07-18T08:00:00-03:00")
    db.Base.metadata.drop_all(db.engine)
    db.init_db()


def create_job():
    with db.state_transaction() as (session, row, state):
        incident = engine.create_incident(state, "collision", "civic_square", 2)
        services.enqueue_incidents(session, row, state)
        ident = incident["id"]
    with db.Session() as session:
        job = session.get(db.Outbox, ident)
        return ident, copy.deepcopy(job.payload), job.body_hash


def receipt(duplicate=False):
    return {"duplicate": duplicate, "call": {"id": CALL_ID, "incident_id": REMOTE_ID},
            "remote_incident_id": REMOTE_ID, "remote_call_id": CALL_ID}


def make_due(ident, expire_lease=False):
    with db.Session.begin() as session:
        job = session.get(db.Outbox, ident)
        job.next_attempt_at = db.now() - timedelta(seconds=1)
        if expire_lease:
            job.lease_until = db.now() - timedelta(seconds=1)


def mark_sent(ident):
    with db.Session.begin() as session:
        job = session.get(db.Outbox, ident)
        job.status = "sent"
        job.remote_incident_id = REMOTE_ID
        job.receipt = receipt()
        job.sent_at = db.now()


def test_claim_lease_excludes_duplicate_workers_and_recovers_expired_claim():
    ident, frozen, digest = create_job()
    first = worker.claim_dispatch()
    assert first["id"] == ident
    assert first["attempts"] == 1
    assert first["payload"] == frozen
    assert worker.claim_dispatch() is None
    make_due(ident, expire_lease=True)
    second = worker.claim_dispatch()
    assert second["attempts"] == 2
    assert second["payload"] == frozen
    with db.Session() as session:
        job = session.get(db.Outbox, ident)
        assert job.body_hash == digest
        assert job.payload == frozen


def test_uncertain_network_receipt_retries_exact_frozen_body(monkeypatch):
    ident, frozen, digest = create_job()
    sent = []
    async def timeout(payload):
        sent.append(copy.deepcopy(payload))
        # Obtaining the clock write lock here proves send is outside its transaction.
        with db.state_transaction() as (_session, _row, _state):
            pass
        raise BridgeError("Simulator connection unavailable", retryable=True)
    monkeypatch.setattr(worker, "send_envelope", timeout)
    assert asyncio.run(worker.dispatch_once()) is True
    with db.Session() as session:
        job = session.get(db.Outbox, ident)
        assert job.status == "retry"
        assert job.attempts == 1
        assert job.lease_until is None
        assert job.payload == frozen
        assert job.body_hash == digest
        assert session.get(db.Simulation, 1).payload["incidents"][0]["dispatch_status"] == "retry"
    assert asyncio.run(worker.dispatch_once()) is False
    async def accepted(payload):
        sent.append(copy.deepcopy(payload))
        return receipt(duplicate=True)
    monkeypatch.setattr(worker, "send_envelope", accepted)
    make_due(ident)
    assert asyncio.run(worker.dispatch_once()) is True
    assert sent == [frozen, frozen]
    with db.Session() as session:
        job = session.get(db.Outbox, ident)
        assert job.status == "sent"
        assert job.attempts == 2
        assert job.remote_incident_id == REMOTE_ID
        assert job.receipt["duplicate"] is True
        assert job.body_hash == digest
        assert job.error is None and job.lease_until is None
        incident = session.get(db.Simulation, 1).payload["incidents"][0]
        assert incident["dispatch_status"] == "sent"
        assert incident["remote_incident_id"] == REMOTE_ID


def test_crash_after_remote_acceptance_recovers_with_identical_identity(monkeypatch):
    ident, frozen, digest = create_job()
    sent = []
    async def crash_after_acceptance(payload):
        sent.append(copy.deepcopy(payload))
        raise RuntimeError("Simulated crash after upstream acceptance")
    monkeypatch.setattr(worker, "send_envelope", crash_after_acceptance)
    with pytest.raises(RuntimeError, match="Simulated crash"):
        asyncio.run(worker.dispatch_once())
    assert worker.claim_dispatch() is None
    with db.Session() as session:
        job = session.get(db.Outbox, ident)
        assert job.status == "pending"
        assert job.lease_until is not None
        assert job.receipt is None
    async def duplicate_receipt(payload):
        sent.append(copy.deepcopy(payload))
        return receipt(duplicate=True)
    monkeypatch.setattr(worker, "send_envelope", duplicate_receipt)
    make_due(ident, expire_lease=True)
    assert asyncio.run(worker.dispatch_once()) is True
    assert sent == [frozen, frozen]
    with db.Session() as session:
        job = session.get(db.Outbox, ident)
        assert job.status == "sent"
        assert job.body_hash == digest
        assert job.attempts == 2


def test_permanent_error_blocks_until_explicit_retry(monkeypatch):
    ident, frozen, digest = create_job()
    async def rejected(_payload):
        raise BridgeError("Simulator returned HTTP 409", retryable=False, status=409)
    monkeypatch.setattr(worker, "send_envelope", rejected)
    assert asyncio.run(worker.dispatch_once()) is True
    make_due(ident)
    assert asyncio.run(worker.dispatch_once()) is False
    with db.Session() as session:
        job = session.get(db.Outbox, ident)
        assert job.status == "blocked"
        assert job.attempts == 1
        assert job.payload == frozen and job.body_hash == digest


def test_feedback_records_once_and_keeps_hospital_completion_clinical(monkeypatch):
    ident, _frozen, _digest = create_job()
    mark_sent(ident)
    async def observe(remote_id, transit_id):
        assert remote_id == REMOTE_ID and transit_id == ident
        return [
            {"source": "911", "external_id": "stable-command-observation", "incident_id": ident,
             "status": "on_scene", "source_sim_time": "2026-10-02T12:00:00+00:00", "details": {"scope": "command_incident"}},
            {"source": "hospital", "external_id": "stable-hospital-observation", "incident_id": ident,
             "status": "resolved", "source_sim_time": "2026-12-02T12:00:00+00:00",
             "details": {"scope": "ems_journey", "remote_status": "completed", "paused": True}},
        ]
    monkeypatch.setattr(worker, "observe_incident", observe)
    asyncio.run(worker.feedback_once())
    asyncio.run(worker.feedback_once())
    with db.Session() as session:
        assert session.scalar(select(func.count()).select_from(db.Report)) == 2
        incident = session.get(db.Simulation, 1).payload["incidents"][0]
        assert incident["status"] == "on_scene"
        assert incident["clinical_status"] == "resolved"
        assert len(incident["feedback"]) == 2
        assert session.get(db.Outbox, ident).observed_at is not None
        records = session.scalars(select(db.Report)).all()
        assert all(r.payload["schema_version"] == "1.0" and r.payload["synthetic"] is True for r in records)
        assert records[0].payload["source_sim_time"] != records[1].payload["source_sim_time"]


def test_failed_feedback_keeps_incident_unresolved_and_rotates_poll(monkeypatch):
    ident, _frozen, _digest = create_job()
    mark_sent(ident)
    async def unavailable(_remote_id, _transit_id):
        raise BridgeError("Simulator connection unavailable", retryable=True)
    monkeypatch.setattr(worker, "observe_incident", unavailable)
    asyncio.run(worker.feedback_once())
    with db.Session() as session:
        assert session.get(db.Outbox, ident).observed_at is not None
        assert session.scalar(select(func.count()).select_from(db.Report)) == 0
        assert session.get(db.Simulation, 1).payload["incidents"][0]["status"] == "reported"


def test_connection_snapshot_preserves_pause_and_capacity(monkeypatch):
    async def connections():
        return [
            {"id": "911", "source": "911", "status": "ok", "reachable": True, "dispatch_configured": True},
            {"id": "hospital", "source": "hospital", "status": "ok", "reachable": True, "paused": True,
             "source_sim_time": "2026-12-02T12:00:00+00:00", "departments": [{"id": "emergency", "occupancy_pct": 140}],
             "ems": {"fleet_size": 8, "busy_units": 8}},
        ]
    monkeypatch.setattr(worker, "connection_status", connections)
    asyncio.run(worker.connections_once())
    with db.Session() as session:
        hospital = session.get(db.Connection, "hospital")
        assert hospital.status == "ok"
        assert hospital.details["paused"] is True
        assert hospital.details["departments"][0]["occupancy_pct"] == 140
        assert hospital.details["ems"]["busy_units"] == 8
        assert session.scalar(select(func.count()).select_from(db.Connection)) == 2


def test_feedback_attempt_deadline_schedules_backoff_and_releases_lease(monkeypatch):
    ident, _frozen, _digest = create_job();mark_sent(ident)
    async def timeout(_remote, _transit):
        raise asyncio.TimeoutError()
    monkeypatch.setattr(worker,'observe_incident',timeout)
    assert asyncio.run(worker.feedback_once())==1
    with db.Session() as session:
        row=session.get(db.FeedbackPoll,ident)
        assert row.state=='backoff' and row.failures==1 and row.lease_until is None
        assert '90-second' in row.last_error
    assert asyncio.run(worker.feedback_once())==0


def test_worker_passes_shared_client_and_explicit_wake_refresh(monkeypatch):
    ident, _frozen, _digest=create_job();mark_sent(ident)
    worker.claim_feedback()  # Install the schedule, then emulate an expired claim.
    with db.Session.begin() as session:
        row=session.get(db.FeedbackPoll,ident);row.lease_until=db.now()-timedelta(seconds=1)
        services.wake_feedback(session,ident)
    sentinel=object();seen=[]
    async def observation(remote,transit,**kwargs):
        seen.append(kwargs)
        return [{'source':'911','external_id':'shared-client-observation','incident_id':transit,'status':'on_scene',
                 'details':{'scope':'command_incident','assignments':[]}}]
    monkeypatch.setattr(worker,'observe_incident',observation)
    assert asyncio.run(worker.feedback_once(sentinel))==1
    assert seen==[{'force_refresh':True,'client':sentinel}]


def test_slow_feedback_does_not_block_independent_dispatch_or_connection_loops(monkeypatch):
    async def scenario():
        feedback_started=asyncio.Event();release=asyncio.Event();health=asyncio.Event()
        called={'dispatch':0,'connections':0};sentinel=object()
        async def feedback(client):
            assert client is sentinel
            feedback_started.set()
            await release.wait()
        async def dispatch(client):
            assert client is sentinel
            called['dispatch']+=1
            return False
        async def connections(client):
            assert client is sentinel
            called['connections']+=1
        monkeypatch.setattr(worker,'feedback_once',feedback)
        monkeypatch.setattr(worker,'dispatch_once',dispatch)
        monkeypatch.setattr(worker,'connections_once',connections)
        tasks=[asyncio.create_task(worker._dispatch_loop(sentinel)),
               asyncio.create_task(worker._feedback_loop(sentinel,health)),
               asyncio.create_task(worker._connections_loop(sentinel,health))]
        try:
            await asyncio.wait_for(feedback_started.wait(),1)
            assert called['dispatch']==1 and called['connections']==1
            assert not release.is_set()
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
    asyncio.run(scenario())


def test_connection_snapshot_keeps_actual_health_observation_time(monkeypatch):
    async def connection():
        return [{'id':'911','status':'ok','observed_at':'2026-09-01T12:00:00+00:00',
                 'checked_at':'2026-09-28T15:00:00+00:00'}]
    monkeypatch.setattr(worker,'connection_status',connection)
    asyncio.run(worker.connections_once())
    with db.Session() as session:
        row=session.get(db.Connection,'911')
        assert row.observed_at.isoformat().startswith('2026-09-01T12:00:00')
        assert row.details['checked_at']=='2026-09-28T15:00:00+00:00'


def test_worker_discards_observation_if_another_worker_replaced_its_lease(monkeypatch):
    ident,_frozen,_digest=create_job();mark_sent(ident)
    async def stale_observation(remote,transit):
        with db.Session.begin() as session:
            row=session.get(db.FeedbackPoll,ident)
            row.details={**row.details,'lease_token':'new-owner-token'}
        return [{'source':'911','external_id':'stale-observation','incident_id':transit,'status':'resolved',
                 'details':{'scope':'command_incident','assignments':[]}}]
    monkeypatch.setattr(worker,'observe_incident',stale_observation)
    assert asyncio.run(worker.feedback_once())==1
    with db.Session() as session:
        assert session.scalar(select(func.count()).select_from(db.Report))==0
        assert session.get(db.FeedbackPoll,ident).details['lease_token']=='new-owner-token'
        assert session.get(db.Simulation,1).payload['incidents'][0]['status']=='reported'
