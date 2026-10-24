"""REST/domain boundaries against a disposable database; no live bridge traffic."""
import copy
import os
import tempfile
from pathlib import Path

# Set the database before app.db can construct an engine. This suite must never
# inherit a workstation PostgreSQL URL or the persistent development database.
_DATABASE_DIR = tempfile.TemporaryDirectory(prefix="northstar-transit-api-tests-")
_DATABASE_FILE = Path(_DATABASE_DIR.name) / "api.sqlite"
os.environ["DATABASE_URL"] = "sqlite:///" + str(_DATABASE_FILE)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import bridge, db, engine, services
from app.main import app

CONTROL = {"X-Transit-Token": "test-control-secret"}
REPORT = {"X-Transit-Token": "test-report-secret"}


def test_new_dispatch_uses_confirmed_precise_location_capability(client):
    with db.Session.begin() as session:
        session.add(db.Connection(id='911', status='ok', details={'supports_location': True}))
    incident = scenario(client, place_id='northstar_airport')
    with db.Session() as session:
        job = session.get(db.Outbox, incident['id'])
        assert job.payload['call']['location'] == incident['location']
        assert job.payload['call']['location']['x'] == 15500


def test_capability_upgrade_never_changes_existing_dispatch_body(client):
    incident = scenario(client)
    with db.Session() as session:
        job = session.get(db.Outbox, incident['id'])
        original = copy.deepcopy(job.payload)
        original_hash = job.body_hash
    with db.state_transaction() as (session, row, state):
        session.add(db.Connection(id='911', status='ok', details={'supports_location': True}))
        session.flush()
        services.enqueue_incidents(session, row, state)
    with db.Session() as session:
        job = session.get(db.Outbox, incident['id'])
        assert job.payload == original
        assert job.body_hash == original_hash
        assert 'location' not in job.payload['call']


@pytest.fixture
def client(monkeypatch):
    assert str(db.engine.url.database) == str(_DATABASE_FILE), "Refusing to alter a non-test database"
    monkeypatch.setenv("TRANSIT_ADMIN_TOKEN", "test-control-secret")
    monkeypatch.setenv("TRANSIT_REPORT_TOKEN", "test-report-secret")
    monkeypatch.setenv("TRANSIT_UI_ORIGINS", "http://127.0.0.1:8825,http://localhost:8825")
    monkeypatch.setenv("SIMULATION_SEED", "42")
    monkeypatch.setenv("SIMULATION_START", "2026-07-18T08:00:00-03:00")
    db.Base.metadata.drop_all(db.engine)
    # A TestClient starts only API lifespan initialization, never worker loops.
    with TestClient(app, base_url="http://127.0.0.1:8825", raise_server_exceptions=False) as browser:
        yield browser


def scenario(client, **changes):
    response = client.post("/api/scenarios", headers=CONTROL, json={
        "kind": "collision", "place_id": "civic_square", "casualties": 2, **changes})
    assert response.status_code == 201, response.text
    return response.json()


def feedback(transit_id, **changes):
    return {"schema_version": "1.0", "synthetic": True, "source": "911", "external_id": "event-1",
            "incident_id": transit_id, "kind": "incident_status", "status": "on_scene",
            "source_sim_time": "2026-10-01T12:00:00Z", "details": {"scope": "command_incident"}, **changes}


def get_incident(client, ident, run_id=None):
    body = client.get(f"/api/runs/{run_id}" if run_id else "/api/incidents").json()
    return next(i for i in body.get("incidents", body.get("items", [])) if i["id"] == ident)


def test_read_only_bootstrap_and_no_dispatch_side_effects(client):
    dashboard = client.get("/api/dashboard")
    assert dashboard.status_code == 200
    assert dashboard.json()["synthetic"] is True
    assert dashboard.json()["stats"]["active_people"] > 0
    assert client.get("/api/health").json()["worker"] == "stale"
    assert client.get("/api/integrations/status").json()["outbox"] == []
    assert dashboard.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path,body", [
    ("/api/control", {"paused": True}),
    ("/api/scenarios", {"kind": "medical", "place_id": "civic_square", "casualties": 1}),
    ("/api/scenarios/jump", {"date": "2026-08-01"}),
    ("/api/integrations/reports", {"kind": "capacity", "source": "hospital", "external_id": "test", "synthetic": True}),
])
def test_mutations_require_credentials(client, path, body):
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, json=body, headers={"X-Transit-Token": "wrong"}).status_code == 401


def test_report_and_control_credentials_have_separate_scope(client):
    assert client.post("/api/control", json={"paused": True}, headers=REPORT).status_code == 401
    body = {"synthetic": True, "kind": "capacity", "source": "hospital", "external_id": "capacity-1"}
    assert client.post("/api/integrations/reports", json=body, headers=CONTROL).status_code == 401
    assert client.post("/api/integrations/reports", json=body, headers=REPORT).status_code == 201


def test_scenario_and_frozen_outbox_are_atomic_and_immutable(client):
    incident = scenario(client)
    with db.Session() as session:
        row = session.get(db.Simulation, 1)
        job = session.get(db.Outbox, incident["id"])
        frozen = copy.deepcopy(job.payload)
        digest = job.body_hash
        assert any(i["id"] == job.id for i in row.payload["incidents"])
        assert job.run_id == row.run_id
        assert job.status == "pending"
        assert job.attempts == 0
        assert digest == services.body_hash(frozen)
        assert frozen["external_id"] == incident["id"]
        assert "location" not in frozen["call"]
        assert frozen["call"]["district_id"] == incident["district_id"]
    assert client.post("/api/integrations/reports", headers=REPORT, json=feedback(incident["id"])).status_code == 201
    assert client.post(f"/api/integrations/outbox/{incident['id']}/retry", headers=CONTROL).status_code == 200
    with db.state_transaction() as (session, row, state):
        services.enqueue_incidents(session, row, state)
    with db.Session() as session:
        job = session.get(db.Outbox, incident["id"])
        assert job.payload == frozen
        assert job.body_hash == digest
        assert session.scalar(select(func.count()).select_from(db.Outbox)) == 1


def test_failed_enqueue_rolls_back_incident_and_outbox(client, monkeypatch):
    before = client.get("/api/incidents").json()
    def fail(_incident):
        raise ValueError("Forced outbox construction failure")
    monkeypatch.setattr(bridge, "build_envelope", fail)
    response = client.post("/api/scenarios", headers=CONTROL,
        json={"kind": "collision", "place_id": "civic_square", "casualties": 1})
    assert response.status_code == 500
    assert client.get("/api/incidents").json() == before
    assert client.get("/api/integrations/status").json()["outbox"] == []


def test_signal_fault_has_local_effect_without_emergency_outbox(client):
    incident = scenario(client, kind="signal_failure", casualties=0)
    assert incident["transport_impact"] == "service_stopped"
    assert incident["requires_911"] is False
    assert client.get("/api/integrations/status").json()["outbox"] == []


def test_feedback_exact_replay_and_conflict(client):
    incident = scenario(client)
    body = feedback(incident["id"])
    first = client.post("/api/integrations/reports", headers=REPORT, json=body)
    assert first.status_code == 201
    duplicate = client.post("/api/integrations/reports", headers=REPORT, json=body)
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["id"] == first.json()["id"]
    changed = {**body, "status": "resolved"}
    assert client.post("/api/integrations/reports", headers=REPORT, json=changed).status_code == 409
    current = get_incident(client, incident["id"])
    assert current["status"] == "on_scene"
    assert len(current["feedback"]) == 1
    stored = client.get("/api/integrations/reports").json()["items"]
    assert len(stored) == 1
    assert stored[0]["source_sim_time"] == "2026-10-01T12:00:00Z"
    assert stored[0]["observed_at"]
    assert stored[0]["transit_sim_time"] != stored[0]["source_sim_time"]


def test_hospital_completion_preserves_clinical_outcome_without_resolving_city(client):
    incident = scenario(client)
    client.post("/api/integrations/reports", headers=REPORT, json=feedback(incident["id"]))
    hospital = feedback(incident["id"], source="hospital", external_id="ems-completed", status="resolved",
                        details={"scope": "ems_journey", "remote_status": "completed", "patient_id": "synthetic-patient"})
    assert client.post("/api/integrations/reports", headers=REPORT, json=hospital).status_code == 201
    current = get_incident(client, incident["id"])
    assert current["status"] == "on_scene"
    assert current["clinical_status"] == "resolved"
    assert current["feedback"][-1]["status"] == "resolved"
    command = feedback(incident["id"], external_id="911-completed", status="resolved")
    assert client.post("/api/integrations/reports", headers=REPORT, json=command).status_code == 201
    assert get_incident(client, incident["id"])["status"] == "resolved"


def test_date_jump_archives_outbox_and_accepts_late_feedback(client):
    old = scenario(client)
    old_run = client.get("/api/health").json()["run_id"]
    initial = feedback(old["id"])
    assert client.post("/api/integrations/reports", headers=REPORT, json=initial).status_code == 201
    with db.Session() as session:
        original_body = copy.deepcopy(session.get(db.Outbox, old["id"]).payload)
    jumped = client.post("/api/scenarios/jump", headers=CONTROL, json={"date": "2026-12-01", "seed": 42})
    assert jumped.status_code == 200
    new_run = jumped.json()["run_id"]
    assert old_run != new_run
    assert client.get("/api/incidents").json()["items"] == []
    assert old_run in [r["id"] for r in client.get("/api/runs").json()["archived"]]
    late = feedback(old["id"], external_id="late-final", status="resolved")
    assert client.post("/api/integrations/reports", headers=REPORT, json=late).status_code == 201
    assert get_incident(client, old["id"], old_run)["status"] == "resolved"
    assert client.get("/api/incidents").json()["items"] == []
    new = scenario(client)
    assert new["id"] != old["id"]
    with db.Session() as session:
        job = session.get(db.Outbox, old["id"])
        assert job.payload == original_body
        assert job.run_id == old_run
        records = session.scalars(select(db.Report)).all()
        assert len(records) == 2
        assert all(r.run_id == old_run for r in records)
        assert session.get(db.Outbox, new["id"]).run_id == new_run


@pytest.mark.parametrize("changes", [
    {"place_id": "unknown-place"}, {"route_id": "unknown-route"}, {"route_id": ""},
    {"casualties": -1}, {"casualties": 31}, {"casualties": True}, {"casualties": "3"},
    {"casualties": 1.5}, {"kind": "unknown-kind"}, {"unknown": "forbidden"},
])
def test_invalid_scenarios_do_not_mutate_state(client, changes):
    response = client.post("/api/scenarios", headers=CONTROL,
        json={"kind": "collision", "place_id": "civic_square", "casualties": 1, **changes})
    assert response.status_code == 422, response.text
    assert client.get("/api/incidents").json()["items"] == []
    assert client.get("/api/integrations/status").json()["outbox"] == []


def test_report_validation_and_unknown_incident(client):
    assert client.post("/api/integrations/reports", headers=REPORT, json=feedback("unknown")).status_code == 404
    for changes in ({"incident_id": None}, {"source_sim_time": "2026-07-18T12:00:00"},
                    {"synthetic": False}, {"synthetic": 1}, {"synthetic": "true"},
                    {"details": {"oversized": "x" * 65537}}):
        response = client.post("/api/integrations/reports", headers=REPORT, json=feedback("unknown", **changes))
        assert response.status_code == 422, response.text
    assert client.get("/api/integrations/reports").json()["items"] == []


def test_pause_speed_bounds_and_step_behavior(client):
    response = client.post("/api/control", headers=CONTROL, json={"paused": True, "speed": 300})
    assert response.status_code == 200
    assert response.json()["clock"]["paused"] is True
    assert response.json()["clock"]["speed"] == 300
    before = response.json()["clock"]["iso"]
    from app.worker import tick
    tick(1)
    assert client.get("/api/dashboard").json()["clock"]["iso"] == before
    for value in (0, -1, 2, 10000):
        assert client.post("/api/control", headers=CONTROL, json={"speed": value}).status_code == 422
    assert client.post("/api/control", headers=CONTROL, json={"paused": "false"}).status_code == 422
    assert client.post("/api/control", headers=CONTROL, json={"paused": False, "speed": 1}).status_code == 200
    tick(1)
    assert client.get("/api/dashboard").json()["clock"]["iso"] != before


def test_session_requires_exact_approved_origin_and_custom_header(client):
    valid = {"Origin": "http://127.0.0.1:8825", "X-Requested-With": "NorthstarDashboard", "Sec-Fetch-Site": "same-origin"}
    for headers in ({}, {**valid, "Origin": "http://evil.test"},
                    {**valid, "Origin": "http://localhost:8825"},
                    {**valid, "Origin": "http://127.0.0.1:8825.attacker.test"},
                    {**valid, "X-Requested-With": "other"}, {**valid, "Sec-Fetch-Site": "cross-site"}):
        assert client.post("/api/session", headers=headers).status_code == 403
    response = client.post("/api/session", headers=valid)
    assert response.status_code == 200
    token = response.json()["token"]
    assert token not in {CONTROL["X-Transit-Token"], REPORT["X-Transit-Token"]}
    session_headers = {"X-Transit-Token": token}
    assert client.post("/api/control", headers=session_headers, json={"paused": True}).status_code == 200
    assert client.post("/api/integrations/reports", headers=session_headers,
        json={"kind": "capacity", "source": "hospital", "external_id": "session-denied", "synthetic": True}).status_code == 401
    with db.Session() as session:
        rows = session.scalars(select(db.UISession)).all()
        assert len(rows) == 1
        assert rows[0].token_hash != token


def test_jump_and_list_validation(client):
    for body in ({"date": "2019-01-01"}, {"date": "2101-01-01"}, {"date": "2026-01-01", "seed": -1}):
        assert client.post("/api/scenarios/jump", headers=CONTROL, json=body).status_code == 422
    assert client.get("/api/people?limit=501").status_code == 422
    assert client.get("/api/events?month=13").status_code == 422
    assert client.get("/api/integrations/reports?limit=201").status_code == 422
    assert client.get("/api/runs/unknown").status_code == 404


def test_utility_and_traffic_reads_are_consistent_without_side_effects(client):
    with db.Session() as session:
        before = copy.deepcopy(session.get(db.Simulation, 1).payload)
    whole = client.get('/api/infrastructure').json()
    assert whole['synthetic'] is True
    assert whole['facilities']
    assert whole['summary']['energy_kwh'] == 0
    facility = whole['facilities'][0]
    detail = client.get('/api/infrastructure/facilities/' + facility['id'])
    assert detail.status_code == 200
    assert detail.json()['facility'] == facility
    assert detail.json()['run_id'] == whole['run_id']
    assert client.get('/api/infrastructure/facilities/unknown').status_code == 404
    dashboard = client.get('/api/dashboard').json()
    traffic = client.get('/api/road-traffic?limit=2&offset=1').json()
    assert traffic['vehicles'] == dashboard['road_traffic']['vehicles'][1:3]
    assert traffic['total'] == len(dashboard['road_traffic']['vehicles'])
    assert traffic['clock'] == dashboard['clock']
    assert client.get('/api/road-traffic?limit=501').status_code == 422
    with db.Session() as session:
        assert session.get(db.Simulation, 1).payload == before


@pytest.mark.parametrize('headers', [{}, REPORT, {'X-Transit-Token': 'wrong'}])
def test_utility_controls_require_control_scope(client, headers):
    body = {'facility_id': 'northstar_airport', 'utility': 'power', 'duration_minutes': 10}
    assert client.post('/api/infrastructure/disruptions', headers=headers, json=body).status_code == 401
    assert client.post('/api/infrastructure/disruptions/unknown/restore', headers=headers).status_code == 401


def test_utility_disrupt_restore_persists_without_emergency_dispatch(client):
    body = {'facility_id': 'northstar_airport', 'utility': 'internet', 'duration_minutes': 10, 'severity': .75}
    response = client.post('/api/infrastructure/disruptions', headers=CONTROL, json=body)
    assert response.status_code == 201, response.text
    disruption = response.json()['disruption']
    ident = disruption['id']
    current = client.get('/api/infrastructure').json()
    assert any(d['id'] == ident for d in current['disruptions'])
    assert current['summary']['active_disruptions'] == 1
    assert client.get('/api/integrations/status').json()['outbox'] == []
    # Restart-style initialization preserves both the run and installed meters.
    db.init_db()
    assert client.get('/api/infrastructure').json() == current
    assert client.post('/api/infrastructure/disruptions', headers=CONTROL, json=body).status_code == 422
    restored = client.post(f'/api/infrastructure/disruptions/{ident}/restore', headers=CONTROL)
    assert restored.status_code == 200
    assert client.get('/api/infrastructure').json()['summary']['active_disruptions'] == 0
    assert client.post(f'/api/infrastructure/disruptions/{ident}/restore', headers=CONTROL).json() == restored.json()
    assert client.post('/api/infrastructure/disruptions/unknown/restore', headers=CONTROL).status_code == 404
    with db.Session() as session:
        assert session.scalar(select(func.count()).select_from(db.Outbox)) == 0


@pytest.mark.parametrize('changes', [
    {'utility': 'gas'}, {'duration_minutes': 0}, {'duration_minutes': 1441},
    {'duration_minutes': True}, {'duration_minutes': '30'}, {'severity': 0},
    {'severity': 1.01}, {'severity': True}, {'severity': 'NaN'}, {'extra': 1},
])
def test_invalid_utility_scenarios_rejected_without_state_change(client, changes):
    before = client.get('/api/infrastructure').json()
    body = {'facility_id': 'northstar_airport', 'utility': 'power', 'duration_minutes': 30, **changes}
    assert client.post('/api/infrastructure/disruptions', headers=CONTROL, json=body).status_code == 422
    assert client.get('/api/infrastructure').json() == before


def test_unknown_utility_facility_rolls_back(client):
    before = client.get('/api/infrastructure').json()
    response = client.post('/api/infrastructure/disruptions', headers=CONTROL,
        json={'facility_id': 'unknown', 'utility': 'water'})
    assert response.status_code == 404
    assert client.get('/api/infrastructure').json() == before


def test_additive_migration_preserves_old_run_and_starts_zero_meters(client):
    with db.state_transaction() as (_session, row, state):
        state.pop('infrastructure', None)
        state.pop('road_traffic', None)
        before = copy.deepcopy(state)
    db.init_db()
    with db.Session() as session:
        after = session.get(db.Simulation, 1).payload
        for key, value in before.items():
            assert after[key] == value, key
    assert client.get('/api/infrastructure').json()['summary']['energy_kwh'] == 0


def sent_poll(client, state='retired'):
    incident = scenario(client)
    with db.Session.begin() as session:
        job = session.get(db.Outbox, incident['id'])
        job.status = 'sent'
        job.remote_incident_id = '11111111-1111-4111-8111-111111111111'
        session.add(db.FeedbackPoll(outbox_id=job.id, state=state, failures=3,
            terminal_since=db.now(), details={'wake_generation': 2, 'interval_seconds': 120}))
    return incident


def test_new_callback_wakes_retired_poll_but_duplicate_does_not(client):
    incident = sent_poll(client)
    body = feedback(incident['id'])
    assert client.post('/api/integrations/reports', headers=REPORT, json=body).status_code == 201
    with db.Session.begin() as session:
        poll = session.get(db.FeedbackPoll, incident['id'])
        assert poll.state == 'active' and poll.failures == 0 and poll.terminal_since is None
        assert poll.details['wake_generation'] == 3
        poll.state = 'retired'
    assert client.post('/api/integrations/reports', headers=REPORT, json=body).status_code == 200
    with db.Session() as session:
        poll = session.get(db.FeedbackPoll, incident['id'])
        assert poll.state == 'retired' and poll.details['wake_generation'] == 3


def test_manual_feedback_refresh_preserves_dispatch_and_scoped_auth(client):
    incident = sent_poll(client)
    ident = incident['id']
    endpoint = f'/api/integrations/outbox/{ident}/refresh'
    with db.Session() as session:
        job = session.get(db.Outbox, ident)
        frozen, digest, attempts = copy.deepcopy(job.payload), job.body_hash, job.attempts
    assert client.post(endpoint).status_code == 401
    assert client.post(endpoint, headers=REPORT).status_code == 401
    assert client.post(endpoint, headers=CONTROL).json() == {'status': 'queued', 'incident_id': ident}
    with db.Session() as session:
        poll = session.get(db.FeedbackPoll, ident)
        assert poll.state == 'active' and poll.details['wake_generation'] == 3
        job = session.get(db.Outbox, ident)
        assert job.payload == frozen and job.body_hash == digest and job.attempts == attempts
        assert job.status == 'sent'
    pending = scenario(client)
    assert client.post(f"/api/integrations/outbox/{pending['id']}/refresh", headers=CONTROL).status_code == 409
    assert client.post('/api/integrations/outbox/missing/refresh', headers=CONTROL).status_code == 404


def test_polling_diagnostics_reads_never_wake_or_mutate_jobs(client):
    incident = sent_poll(client)
    before = client.get('/api/integrations/status').json()
    after = client.get('/api/integrations/status').json()
    assert before == after
    assert before['polling']['counts'] == {'retired': 1}
    assert before['polling']['awaiting_schedule'] == 0
    row = next(j for j in before['outbox'] if j['id'] == incident['id'])
    assert row['feedback_poll']['state'] == 'retired'
    assert row['feedback_poll']['next_poll_at'] is None
    with db.Session() as session:
        assert session.get(db.FeedbackPoll, incident['id']).details['wake_generation'] == 2


def test_callback_before_first_poll_creates_explicit_wake(client):
    incident = scenario(client)
    with db.Session.begin() as session:
        job = session.get(db.Outbox, incident['id'])
        job.status = 'sent'
        job.remote_incident_id = '11111111-1111-4111-8111-111111111111'
    assert client.post('/api/integrations/reports', headers=REPORT,
        json=feedback(incident['id'])).status_code == 201
    with db.Session() as session:
        poll = session.get(db.FeedbackPoll, incident['id'])
        assert poll.state == 'active' and poll.details['wake_generation'] == 1


def test_feedback_schedule_table_upgrade_preserves_existing_simulation_and_dispatch(client):
    incident = scenario(client)
    with db.Session() as session:
        before_state = copy.deepcopy(session.get(db.Simulation, 1).payload)
        job = session.get(db.Outbox, incident['id'])
        before_dispatch = (copy.deepcopy(job.payload), job.body_hash, job.status)
    # Mimic an installed database predating the additive schedule table.
    db.FeedbackPoll.__table__.drop(db.engine)
    db.init_db()
    with db.Session() as session:
        assert session.get(db.Simulation, 1).payload == before_state
        job = session.get(db.Outbox, incident['id'])
        assert (job.payload, job.body_hash, job.status) == before_dispatch
        assert session.scalar(select(func.count()).select_from(db.FeedbackPoll)) == 0


def test_cruise_terminal_endpoint_reports_scheduled_calls(client):
    response = client.get('/api/cruise-terminal')
    assert response.status_code == 200
    body = response.json()
    assert body['enabled'] is True and body['synthetic'] is True
    assert body['port'] == 'northstar_harbour'
    # The timetable also lists the next day's calls; check the first day's two.
    first_day = body['ships'][0]['arrival_iso'][:10]
    today = [ship for ship in body['ships'] if ship['arrival_iso'].startswith(first_day)]
    assert [ship['status'] for ship in today] == ['scheduled', 'scheduled']
    assert today[0]['ship'] == 'MV Aurora Atlantic'
    assert set(body['queues']) >= {'customs_people', 'security_people'}
    assert {destination['id'] for destination in body['destinations']} == \
        {'harbour_market', 'marina', 'coastal_beach'}


def test_cruise_terminal_scenario_control_requires_token_and_validates(client):
    assert client.post('/api/cruise-terminal/scenario', json={'berths': 1}).status_code == 401
    assert client.post('/api/cruise-terminal/scenario',
                       headers=CONTROL, json={'berths': 99}).status_code == 422
    assert client.post('/api/cruise-terminal/scenario',
                       headers=CONTROL, json={}).status_code == 422
    response = client.post('/api/cruise-terminal/scenario',
                           headers=CONTROL, json={'berths': 1, 'customs_lanes': 2})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['config']['berths'] == 1 and body['config']['customs_lanes'] == 2
    assert any(event['kind'] == 'scenario' for event in body['events'])
