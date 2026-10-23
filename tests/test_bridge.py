"""Behavioral adapter tests: no requests reach sibling simulators."""
import asyncio
import copy
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import bridge

COMMAND_ID = "11111111-1111-4111-8111-111111111111"
CALL_ID = "22222222-2222-4222-8222-222222222222"
EMS_ID = "33333333-3333-4333-8333-333333333333"
ASSIGNMENT_ID = "44444444-4444-4444-8444-444444444444"


def incident(**changes):
    return {"id": "run-a-collision-001", "type": "collision", "district_id": "central",
            "place_id": "civic_square", "title": "Bus collision", "casualties": 3,
            "severity": "major", "route_id": "bus-1", "x": 1800, "y": 9100, **changes}


def run_with_client(handler, callback):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            return await callback(client)
    return asyncio.run(run())


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(bridge, "_DEFAULT_CACHE", bridge.BridgeCache())
    monkeypatch.setenv("COMMAND_API_URL", "http://command.test")
    monkeypatch.setenv("HOSPITAL_API_URL", "http://hospital.test")
    monkeypatch.setenv("COMMAND_DISPATCH_TOKEN", "synthetic-test-secret")
    monkeypatch.delenv("TRANSIT_DISPATCH_TOKEN", raising=False)


def test_envelope_strict_stable_compatibility_and_triage():
    original = incident(location={"x": 1800, "y": 9100})
    saved = copy.deepcopy(original)
    body = bridge.build_envelope(original)
    assert body == bridge.build_envelope(original)
    assert original == saved
    assert set(body) == {"schema_version", "synthetic", "source", "external_id", "call"}
    assert body["external_id"] == original["id"]
    assert body["synthetic"] is True
    assert body["call"]["kind"] == "road_collision"
    assert "location" not in body["call"]
    assert body["call"]["casualties"] == {"red": 1, "yellow": 2, "green": 0, "black": 0}
    assert body["call"]["summary"].startswith("Synthetic")
    assert body["call"]["district_id"] == "central"


def canonical_location(**changes):
    return {"crs": "northstar-local-m-v1", "units": "m", "x": 1200, "y": 9300,
            "district_id": "central", "precision": "simulated_point", "provenance": "authored_transit_place", **changes}


def test_location_only_included_for_capable_peer_and_does_not_mutate_frozen_v1_body():
    original = incident(location=canonical_location())
    frozen_v1 = bridge.build_envelope(original)
    frozen_copy = copy.deepcopy(frozen_v1)
    current_v2 = bridge.build_envelope(original, supports_location=True)
    assert current_v2["call"]["location"] == original["location"]
    assert current_v2["call"]["location"] is not original["location"]
    assert "location" not in frozen_v1["call"]
    assert frozen_v1 == frozen_copy
    assert current_v2["external_id"] == frozen_v1["external_id"]


@pytest.mark.parametrize("location", [None, {"x": 1200, "y": 9300},
    canonical_location(crs="EPSG:4326"), canonical_location(x=float("nan")),
    canonical_location(x=True), canonical_location(district_id="east"),
    canonical_location(precision="facility", facility_id="northstar_airport"),
    canonical_location(x=500000),
])
def test_capable_peer_location_requires_canonical_validation(location):
    with pytest.raises(ValueError):
        bridge.build_envelope(incident(location=location), supports_location=True)


def capability_document(include_location=True):
    properties = {"kind": {"type": "string"}, "district_id": {"type": "string"}}
    if include_location:
        properties["location"] = {"anyOf": [{"$ref": "#/components/schemas/Location"}, {"type": "null"}]}
    return {"paths": {"/api/integrations/calls": {"post": {"requestBody": {
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/IntegrationCallCreate"}}}}}}},
        "components": {"schemas": {
            "IntegrationCallCreate": {"properties": {"call": {"$ref": "#/components/schemas/CallInput"}}},
            "CallInput": {"properties": properties},
            "Location": {"properties": {"crs": {"const": "northstar-local-m-v1"}, "units": {"const": "m"},
                                        "x": {"type": "number"}, "y": {"type": "number"}}},
        }}}


@pytest.mark.parametrize("include_location", [False, True])
def test_openapi_capability_matches_actual_ingestion_call_schema(include_location):
    paths = []
    def handler(request):
        assert "X-Dispatch-Token" not in request.headers
        paths.append(str(request.url))
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=capability_document(include_location))
        if request.url.host == "command.test":
            return httpx.Response(200, json={"status": "ok", "simulation": {"paused": False}})
        return httpx.Response(503)
    connections = run_with_client(handler, lambda client: bridge.connection_status(client))
    assert connections[0]["supports_location"] is include_location
    assert paths.count("http://command.test/openapi.json") == 1


def test_capability_false_when_openapi_unavailable_preserves_api_reachability():
    def handler(request):
        if request.url.host == "command.test" and request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(503, text="private upstream error")
    connections = run_with_client(handler, lambda client: bridge.connection_status(client))
    assert connections[0]["reachable"] is True
    assert connections[0]["supports_location"] is False
    assert connections[0]["capability_error"] == "Simulator returned HTTP 503"
    assert "private" not in json.dumps(connections)


def test_unrelated_or_malformed_location_schema_is_not_a_capability():
    document = capability_document()
    document["components"]["schemas"]["CallInput"]["properties"].pop("location")
    assert bridge._supports_location(document) is False
    document = capability_document()
    document["components"]["schemas"]["Location"]["properties"]["crs"]["const"] = "EPSG:4326"
    assert bridge._supports_location(document) is False
    assert bridge._supports_location({"paths": None}) is False
    document = capability_document()
    document["components"]["schemas"]["CallInput"] = {"$ref": "#/components/schemas/CallInput"}
    assert bridge._supports_location(document) is False


@pytest.mark.parametrize("health", [
    {"status": "ok", "world": "Northstar", "world_version": "2026.09.27.2", "version": "1.0.0"},
    {"status": "ok", "simulation": {"settings": {"world_version": "2026.09.27.2", "spatial_version": "2026.09.27.2"}}},
])
def test_world_contract_version_is_distinct_from_api_version(health):
    def handler(request):
        if request.url.host == "command.test":
            if request.url.path == "/openapi.json":
                return httpx.Response(200, json={**capability_document(), "info": {"version": "1.0.0"}})
            return httpx.Response(200, json=health)
        return httpx.Response(503)
    command = run_with_client(handler, lambda client: bridge.connection_status(client))[0]
    assert command["world_version"] == "2026.09.27.2"
    assert command["api_version"] == "1.0.0"
    assert command["supports_location"] is True


@pytest.mark.parametrize("kind,expected", [("collision", "road_collision"), ("medical", "medical"),
    ("crowd_surge", "public_event"), ("signal_failure", "power_outage"), ("storm", "storm_damage")])
def test_authored_category_mapping(kind, expected):
    assert bridge.build_envelope(incident(type=kind))["call"]["kind"] == expected


def test_routine_disruptions_do_not_dispatch():
    assert not bridge.should_dispatch(incident(type="signal_failure", casualties=0))
    assert not bridge.should_dispatch(incident(type="crowd_surge", casualties=0, severity="minor"))
    assert not bridge.should_dispatch(incident(type="congestion", casualties=10))
    assert bridge.should_dispatch(incident(type="medical", casualties=1, severity="minor"))
    assert bridge.should_dispatch(incident(type="storm", casualties=0, severity="major"))
    with pytest.raises(ValueError):
        bridge.build_envelope(incident(type="signal_failure", casualties=0))


@pytest.mark.parametrize("count", [-1, 31, True, 1.5, "2"])
def test_casualty_bounds(count):
    with pytest.raises(ValueError):
        bridge.build_envelope(incident(casualties=count))


def test_send_frozen_body_and_receipt(monkeypatch):
    payload = bridge.build_envelope(incident())
    frozen = copy.deepcopy(payload)
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/integrations/calls"
        assert request.headers["X-Dispatch-Token"] == "synthetic-test-secret"
        assert json.loads(request.content) == frozen
        return httpx.Response(201, json={"duplicate": False, "call": {"id": CALL_ID, "incident_id": COMMAND_ID}})
    receipt = run_with_client(handler, lambda client: bridge.send_envelope(payload, client))
    assert receipt["remote_incident_id"] == COMMAND_ID
    assert receipt["remote_call_id"] == CALL_ID
    assert payload == frozen
    assert len(calls) == 1


def test_missing_token_fails_before_network(monkeypatch):
    monkeypatch.delenv("COMMAND_DISPATCH_TOKEN")
    def handler(request):
        pytest.fail("Unconfigured dispatch must not make a request")
    with pytest.raises(bridge.BridgeError, match="not configured") as error:
        run_with_client(handler, lambda client: bridge.send_envelope(bridge.build_envelope(incident()), client))
    assert error.value.retryable is False


def test_token_fallback_and_duplicate_receipt(monkeypatch):
    monkeypatch.setenv("COMMAND_DISPATCH_TOKEN", "")
    monkeypatch.setenv("TRANSIT_DISPATCH_TOKEN", "fallback-test-secret")
    def handler(request):
        assert request.headers["X-Dispatch-Token"] == "fallback-test-secret"
        return httpx.Response(200, json={"duplicate": True, "call": {"id": CALL_ID, "incident_id": COMMAND_ID}})
    assert run_with_client(handler, lambda client: bridge.send_envelope(bridge.build_envelope(incident()), client))["duplicate"] is True


@pytest.mark.parametrize("status,retryable", [(401, False), (409, False), (422, False), (429, True), (503, True), (302, False)])
def test_delivery_failure_classification_and_no_redirect(status, retryable):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="sensitive upstream contents", headers={"Location": "http://other.test/"})
    with pytest.raises(bridge.BridgeError) as error:
        run_with_client(handler, lambda client: bridge.send_envelope(bridge.build_envelope(incident()), client))
    assert error.value.retryable is retryable
    assert error.value.status == status
    assert "sensitive" not in str(error.value)
    assert len(calls) == 1


def test_network_and_invalid_receipt_are_retryable():
    def timeout(request):
        raise httpx.ReadTimeout("secret URL/credential details", request=request)
    with pytest.raises(bridge.BridgeError) as error:
        run_with_client(timeout, lambda client: bridge.send_envelope(bridge.build_envelope(incident()), client))
    assert error.value.retryable
    assert "secret" not in str(error.value)
    def invalid(request):
        return httpx.Response(201, json={"duplicate": False, "call": {"id": "bad", "incident_id": COMMAND_ID}})
    with pytest.raises(bridge.BridgeError) as error:
        run_with_client(invalid, lambda client: bridge.send_envelope(bridge.build_envelope(incident()), client))
    assert error.value.retryable


def command_body(status="dispatched", assignments=None):
    return {"incident": {"id": COMMAND_ID, "status": status, "correlation_id": "command-correlation",
                         "casualties": {"red": 0, "yellow": 1}, "received_at": "2026-10-01T00:00:00Z",
                         "dispatched_at": "2026-10-01T00:03:00Z"},
            "assignments": assignments if assignments is not None else [
                {"id": ASSIGNMENT_ID, "agency": "ems", "remote_id": EMS_ID,
                 "status": "transporting", "remote_status": "transporting", "external_id": "911-ems-1"}]}


def test_hospital_linkage_paused_and_independent_clocks():
    requests = []
    def handler(request):
        requests.append(request)
        assert "X-Dispatch-Token" not in request.headers
        if request.url.host == "command.test":
            return httpx.Response(200, json=command_body())
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"paused": True, "worker": "healthy", "simulated_at": "2026-12-02T00:00:00Z"})
        return httpx.Response(200, json={"incident": {"id": EMS_ID, "status": "arrived_hospital",
            "patient_id": "patient-1", "encounter_id": "encounter-1", "arrived_hospital_at": "2026-12-01T23:55:00Z"},
            "encounter": {"id": "encounter-1", "status": "waiting", "department_id": "emergency"}})
    reports = run_with_client(handler, lambda client: bridge.observe_incident(COMMAND_ID, "transit-1", client))
    assert [r["source"] for r in reports] == ["911", "hospital"]
    assert reports[0]["status"] == "dispatching"
    assert reports[0]["source_sim_time"] == "2026-10-01T00:03:00Z"
    assert reports[1]["source_sim_time"] == "2026-12-01T23:55:00Z"
    assert reports[1]["status"] == "transporting"
    assert reports[1]["details"]["paused"] is True
    assert reports[1]["details"]["encounter_status"] == "waiting"
    assert reports[1]["details"]["patient_id"] == "patient-1"
    assert reports[1]["details"]["scope"] == "ems_journey"
    assert len(requests) == 4


def test_empty_assignments_never_imply_resolution():
    def handler(request):
        return httpx.Response(200, json=command_body(assignments=[]))
    reports = run_with_client(handler, lambda client: bridge.observe_incident(COMMAND_ID, "transit-1", client))
    assert len(reports) == 1
    assert reports[0]["status"] == "dispatching"


def test_hospital_unavailable_retains_911_observation():
    def handler(request):
        if request.url.host == "command.test":
            return httpx.Response(200, json=command_body("on_scene"))
        return httpx.Response(503, text="upstream secret")
    reports = run_with_client(handler, lambda client: bridge.observe_incident(COMMAND_ID, "transit-1", client))
    assert len(reports) == 1
    assert reports[0]["status"] == "on_scene"
    assert reports[0]["details"]["hospital_health_unavailable"] is True
    assert reports[0]["details"]["hospital_observation_errors"][0]["retryable"] is True
    assert "upstream secret" not in json.dumps(reports)


def test_hospital_completion_does_not_resolve_command():
    def handler(request):
        if request.url.host == "command.test":
            return httpx.Response(200, json=command_body("on_scene"))
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"paused": False, "worker": "healthy"})
        return httpx.Response(200, json={"incident": {"id": EMS_ID, "status": "completed", "completed_at": "2026-12-02T00:00:00Z"}})
    reports = run_with_client(handler, lambda client: bridge.observe_incident(COMMAND_ID, "transit-1", client))
    assert reports[0]["status"] == "on_scene"
    assert reports[1]["status"] == "resolved"
    assert reports[1]["details"]["scope"] == "ems_journey"


def test_observation_id_stable_across_clock_advancement_but_changes_on_lifecycle():
    clock = ["2026-10-01T00:03:00Z"]
    state = ["dispatched"]
    def handler(request):
        body = command_body(state[0], assignments=[])
        body["incident"]["dispatched_at"] = clock[0]
        return httpx.Response(200, json=body)
    first = run_with_client(handler, lambda client: bridge.observe_incident(COMMAND_ID, "transit-1", client))[0]
    clock[0] = "2026-10-01T00:10:00Z"
    second = run_with_client(handler, lambda client: bridge.observe_incident(COMMAND_ID, "transit-1", client))[0]
    assert first["external_id"] == second["external_id"]
    state[0] = "resolved"
    third = run_with_client(handler, lambda client: bridge.observe_incident(COMMAND_ID, "transit-1", client))[0]
    assert third["external_id"] != first["external_id"]
    assert third["status"] == "resolved"


def test_changed_remote_identity_is_rejected():
    def handler(request):
        body = command_body(assignments=[])
        body["incident"]["id"] = EMS_ID
        return httpx.Response(200, json=body)
    with pytest.raises(bridge.BridgeError, match="identity"):
        run_with_client(handler, lambda client: bridge.observe_incident(COMMAND_ID, "transit-1", client))


def test_connection_status_paused_capacity_and_no_secret_leak():
    def handler(request):
        assert "X-Dispatch-Token" not in request.headers
        if request.url.host == "command.test":
            return httpx.Response(200, json={"status": "ok", "worker": {"status": "healthy"},
                                           "simulation": {"paused": False, "simulated_at": "2026-10-01T00:00:00Z"}})
        if request.url.path == "/api/departments":
            return httpx.Response(200, json={"items": [{"id": "emergency", "capacity": 42, "occupied": 60, "occupancy_pct": 142.9}]})
        if request.url.path == "/api/ems":
            return httpx.Response(200, json={"stats": {"fleet_size": 8, "busy_units": 8}})
        return httpx.Response(200, json={"status": "ok", "worker": "healthy", "paused": True})
    connections = run_with_client(handler, lambda client: bridge.connection_status(client))
    assert connections[0]["dispatch_configured"] is True
    assert connections[1]["paused"] is True
    assert connections[1]["departments"][0]["occupancy_pct"] == 142.9
    assert connections[1]["ems"]["busy_units"] == 8
    assert "synthetic-test-secret" not in json.dumps(connections)


class FakeClock:
    def __init__(self):
        self.seconds = 0.0

    def monotonic(self):
        return self.seconds

    def wall_now(self):
        return datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(seconds=self.seconds)

    def advance(self, seconds):
        self.seconds += seconds

    def cache(self, **kwargs):
        return bridge.BridgeCache(monotonic=self.monotonic, wall_now=self.wall_now, **kwargs)


def simulation_handler(calls, *, command=None, ems=None):
    """Healthy sources with fixed source clocks, distinct from the fake wall clock."""
    def handler(request):
        calls[str(request.url)] += 1
        assert "X-Dispatch-Token" not in request.headers
        if request.url.path == "/api/health":
            if request.url.host == "command.test":
                return httpx.Response(200, json={"status": "ok", "simulation": {
                    "paused": False, "simulated_at": "2026-10-10T00:00:00Z"}})
            return httpx.Response(200, json={"status": "ok", "paused": True, "worker": "healthy",
                                             "simulated_at": "2026-12-02T00:00:00Z"})
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=capability_document())
        if request.url.path == "/api/departments":
            return httpx.Response(200, json={"items": [{"id": "emergency", "capacity": 42}]})
        if request.url.path == "/api/ems":
            return httpx.Response(200, json={"stats": {"fleet_size": 8}})
        if request.url.host == "command.test":
            return httpx.Response(200, json=command() if command else command_body())
        return httpx.Response(200, json=ems() if ems else {"incident": {
            "id": EMS_ID, "status": "transporting", "transport_started_at": "2026-12-01T23:55:00Z"}})
    return handler


def test_endpoint_ttls_and_actual_observation_times():
    clock, calls = FakeClock(), Counter()
    cache = clock.cache()
    async def scenario(client):
        first = await bridge.connection_status(client, cache=cache)
        clock.advance(29)
        cached = await bridge.connection_status(client, cache=cache)
        assert sum(calls.values()) == 5
        assert cached[0]["observed_at"] == first[0]["observed_at"] == "2026-10-01T00:00:00Z"
        assert cached[0]["source_sim_time"] == "2026-10-10T00:00:00Z"
        assert cached[0]["endpoint_observations"]["health"]["age_seconds"] == 29
        assert cached[0]["endpoint_observations"]["health"]["cache_hit"] is True
        clock.advance(1)
        refreshed = await bridge.connection_status(client, cache=cache)
        assert refreshed[0]["observed_at"] == "2026-10-01T00:00:30Z"
        assert calls["http://command.test/api/health"] == 2
        assert calls["http://hospital.test/api/departments"] == 1
        clock.advance(30)
        await bridge.connection_status(client, cache=cache)
        assert calls["http://hospital.test/api/departments"] == 2
        assert calls["http://hospital.test/api/ems"] == 2
        assert calls["http://command.test/openapi.json"] == 1
        clock.advance(3539)
        await bridge.connection_status(client, cache=cache)
        assert calls["http://command.test/openapi.json"] == 1
        clock.advance(1)
        last = await bridge.connection_status(client, cache=cache)
        assert calls["http://command.test/openapi.json"] == 2
        assert last[0]["endpoint_observations"]["openapi"]["observed_at"] == "2026-10-01T01:00:00Z"
    run_with_client(simulation_handler(calls), scenario)


@pytest.mark.parametrize("connections_first", [True, False])
def test_health_cache_shared_between_connections_and_incident_observations(connections_first):
    clock, calls = FakeClock(), Counter()
    cache = clock.cache()
    async def scenario(client):
        async def connections():
            return await bridge.connection_status(client, cache=cache)
        async def observation():
            return await bridge.observe_incident(COMMAND_ID, "transit-1", client, cache=cache)
        if connections_first:
            await connections()
            clock.advance(5)
            reports = await observation()
        else:
            reports = await observation()
            clock.advance(5)
            await connections()
        clock.advance(5)
        await observation()
        assert calls["http://command.test/api/health"] == 1
        assert calls["http://hospital.test/api/health"] == 1
        assert calls[f"http://command.test/api/incidents/{COMMAND_ID}"] == 2
        assert calls[f"http://hospital.test/api/ems/incidents/{EMS_ID}"] == 2
        details = reports[0]["details"]
        assert details["command_paused"] is False and details["hospital_paused"] is True
        assert details["assignments_known"] is True
        assert details["linked_ems_ids"] == [EMS_ID] and details["completed_ems_ids"] == []
        assert details["observation_errors"] == []
        assert reports[1]["source_sim_time"] == "2026-12-01T23:55:00Z"
        assert "observed_at" not in json.dumps(reports)
    run_with_client(simulation_handler(calls), scenario)


def test_failed_health_refresh_never_presents_stale_health_as_current():
    clock, calls, healthy = FakeClock(), Counter(), [True]
    cache = clock.cache()
    normal = simulation_handler(calls)
    def handler(request):
        if request.url.host == "command.test" and request.url.path == "/api/health" and not healthy[0]:
            calls[str(request.url)] += 1
            return httpx.Response(503, text="private service output")
        return normal(request)
    async def scenario(client):
        first = (await bridge.connection_status(client, cache=cache))[0]
        healthy[0] = False
        clock.advance(30)
        failed = (await bridge.connection_status(client, cache=cache))[0]
        assert failed["reachable"] is False and failed["status"] == "unavailable"
        assert "paused" not in failed and "source_sim_time" not in failed
        assert failed["observed_at"] == first["observed_at"]
        assert failed["checked_at"] == "2026-10-01T00:00:30Z"
        assert failed["endpoint_observations"]["health"]["fresh"] is False
        assert failed["endpoint_observations"]["health"]["retry_after_seconds"] == 30
        clock.advance(20)
        cached = (await bridge.connection_status(client, cache=cache))[0]
        assert calls["http://command.test/api/health"] == 2
        assert cached["endpoint_observations"]["health"]["cache_hit"] is True
        assert cached["endpoint_observations"]["health"]["retry_after_seconds"] == 10
        reports = await bridge.observe_incident(COMMAND_ID, "transit-1", client, cache=cache)
        assert reports[0]["details"]["command_paused"] is None
        assert reports[0]["details"]["observation_errors"][0]["endpoint"] == "health"
        assert calls["http://command.test/api/health"] == 2
        assert "private service output" not in json.dumps([failed, cached, reports])
    run_with_client(handler, scenario)


def test_failure_backoff_caps_at_five_minutes_and_resets_after_success():
    clock, attempts, failing = FakeClock(), [], [True]
    cache = clock.cache()
    def handler(request):
        attempts.append(clock.seconds)
        return httpx.Response(503, text="private") if failing[0] else httpx.Response(200, json={"status": "ok"})
    async def scenario(client):
        for index, wait in enumerate((30, 60, 120, 240, 300, 300)):
            with pytest.raises(bridge.BridgeError) as failure:
                await cache.get(client, "http://command.test/api/health", bridge.HEALTH_TTL)
            assert failure.value.observation["retry_after_seconds"] == wait
            assert failure.value.observation["consecutive_failures"] == index + 1
            clock.advance(wait - 1)
            with pytest.raises(bridge.BridgeError) as cached:
                await cache.get(client, "http://command.test/api/health", bridge.HEALTH_TTL)
            assert cached.value.observation["cache_hit"] is True
            assert len(attempts) == index + 1
            clock.advance(1)
        failing[0] = False
        _, metadata = await cache.get(client, "http://command.test/api/health", bridge.HEALTH_TTL)
        assert metadata["consecutive_failures"] == 0 and metadata["fresh"] is True
        clock.advance(30)
        failing[0] = True
        with pytest.raises(bridge.BridgeError) as reset:
            await cache.get(client, "http://command.test/api/health", bridge.HEALTH_TTL)
        assert reset.value.observation["retry_after_seconds"] == 30
    run_with_client(handler, scenario)


def test_negative_command_cache_and_explicit_wakeup_retry():
    clock, calls, failing = FakeClock(), Counter(), [True]
    cache = clock.cache()
    normal = simulation_handler(calls)
    def handler(request):
        if request.url.path == f"/api/incidents/{COMMAND_ID}" and failing[0]:
            calls[str(request.url)] += 1
            return httpx.Response(503)
        return normal(request)
    async def scenario(client):
        for _ in range(2):
            with pytest.raises(bridge.BridgeError):
                await bridge.observe_incident(COMMAND_ID, "transit-1", client, cache=cache)
        assert calls[f"http://command.test/api/incidents/{COMMAND_ID}"] == 1
        failing[0] = False
        reports = await bridge.observe_incident(COMMAND_ID, "transit-1", client, cache=cache, force_refresh=True)
        assert reports[0]["status"] == "dispatching"
        assert calls[f"http://command.test/api/incidents/{COMMAND_ID}"] == 2
    run_with_client(handler, scenario)


def completed_body():
    return {"incident": {"id": EMS_ID, "status": "completed", "completed_at": "2026-12-02T00:00:00Z"},
            "encounter": {"id": "synthetic-encounter", "status": "admitted", "department_id": "emergency"}}


def test_completed_ems_reused_per_transit_id_and_unique_ems_until_ttl():
    clock, calls = FakeClock(), Counter()
    cache = clock.cache()
    def duplicate_assignments():
        body = command_body("resolved")
        body["assignments"].append({**body["assignments"][0], "id": "another-assignment"})
        return body
    async def scenario(client):
        first = await bridge.observe_incident(COMMAND_ID, "transit-1", client, cache=cache)
        assert len(first) == 2
        assert first[0]["details"]["completed_ems_ids"] == [EMS_ID]
        assert first[1]["details"]["observation_scope"] == "ems_completion_snapshot"
        clock.advance(299)
        second = await bridge.observe_incident(COMMAND_ID, "transit-1", client, cache=cache)
        assert second[1] == first[1]
        assert calls[f"http://hospital.test/api/ems/incidents/{EMS_ID}"] == 1
        # The same remote journey must have a distinct local association/report.
        other = await bridge.observe_incident(COMMAND_ID, "transit-2", client, cache=cache)
        assert other[1]["incident_id"] == "transit-2"
        assert other[1]["external_id"] != first[1]["external_id"]
        assert calls[f"http://hospital.test/api/ems/incidents/{EMS_ID}"] == 2
        clock.advance(1)
        third = await bridge.observe_incident(COMMAND_ID, "transit-1", client, cache=cache)
        assert third[1] == first[1]
        assert calls[f"http://hospital.test/api/ems/incidents/{EMS_ID}"] == 3
    run_with_client(simulation_handler(calls, command=duplicate_assignments, ems=completed_body), scenario)


def test_force_refresh_reopens_completed_ems_without_flushing_shared_health():
    clock, calls, reopened = FakeClock(), Counter(), [False]
    cache = clock.cache()
    def ems():
        return {"incident": {"id": EMS_ID, "status": "transporting"}} if reopened[0] else completed_body()
    async def scenario(client):
        first = await bridge.observe_incident(COMMAND_ID, "transit-1", client, cache=cache)
        reopened[0] = True
        unchanged = await bridge.observe_incident(COMMAND_ID, "transit-1", client, cache=cache)
        assert unchanged[1] == first[1]
        refreshed = await bridge.observe_incident(COMMAND_ID, "transit-1", client, cache=cache, force_refresh=True)
        assert refreshed[1]["status"] == "transporting"
        assert refreshed[0]["details"]["completed_ems_ids"] == []
        assert calls["http://command.test/api/health"] == 1
        assert calls["http://hospital.test/api/health"] == 1
        await bridge.observe_incident(COMMAND_ID, "transit-1", client, cache=cache)
        assert calls[f"http://hospital.test/api/ems/incidents/{EMS_ID}"] == 3
    run_with_client(simulation_handler(calls, ems=ems), scenario)


@pytest.mark.parametrize("failure_body", [{"incident": {"id": COMMAND_ID, "status": "completed"}},
                                        {"incident": {"id": EMS_ID, "status": "unknown"}}, None])
def test_failed_revalidation_does_not_reuse_terminal_completion(failure_body):
    clock, calls, failing = FakeClock(), Counter(), [False]
    cache = clock.cache()
    normal = simulation_handler(calls, ems=completed_body)
    def handler(request):
        if request.url.path == f"/api/ems/incidents/{EMS_ID}" and failing[0]:
            calls[str(request.url)] += 1
            return httpx.Response(200, json=failure_body) if failure_body else httpx.Response(503)
        return normal(request)
    async def scenario(client):
        await bridge.observe_incident(COMMAND_ID, "transit-1", client, cache=cache)
        failing[0] = True
        failed = await bridge.observe_incident(COMMAND_ID, "transit-1", client, cache=cache, force_refresh=True)
        again = await bridge.observe_incident(COMMAND_ID, "transit-1", client, cache=cache)
        for reports in (failed, again):
            assert len(reports) == 1
            assert reports[0]["details"]["completed_ems_ids"] == []
            assert reports[0]["details"]["hospital_observation_errors"]
        assert calls[f"http://hospital.test/api/ems/incidents/{EMS_ID}"] == 2
    run_with_client(handler, scenario)


def test_multiple_pending_ems_get_one_observation_each_and_no_completed_inference():
    second_id = "55555555-5555-4555-8555-555555555555"
    clock, calls = FakeClock(), Counter()
    cache = clock.cache()
    def assignments():
        body = command_body("resolved")
        original = body["assignments"][0]
        body["assignments"] += [{**original}, {**original, "id": "second-assignment", "remote_id": second_id}]
        return body
    normal = simulation_handler(calls, command=assignments)
    def handler(request):
        if request.url.path == f"/api/ems/incidents/{second_id}":
            calls[str(request.url)] += 1
            return httpx.Response(200, json={"incident": {"id": second_id, "status": "queued"}})
        return normal(request)
    async def scenario(client):
        for _ in range(2):
            reports = await bridge.observe_incident(COMMAND_ID, "transit-1", client, cache=cache)
            assert len(reports) == 3
            assert reports[0]["details"]["linked_ems_ids"] == sorted([EMS_ID, second_id])
            assert reports[0]["details"]["completed_ems_ids"] == []
        for ems_id in (EMS_ID, second_id):
            assert calls[f"http://hospital.test/api/ems/incidents/{ems_id}"] == 2
    run_with_client(handler, scenario)


def test_concurrent_connection_calls_coalesce_shared_endpoint_reads():
    clock, calls = FakeClock(), Counter()
    cache = clock.cache()
    normal = simulation_handler(calls)
    async def handler(request):
        await asyncio.sleep(0)
        return normal(request)
    async def scenario(client):
        results = await asyncio.gather(*(bridge.connection_status(client, cache=cache) for _ in range(8)))
        assert len(results) == 8
        assert sum(calls.values()) == 5
    run_with_client(handler, scenario)


def test_cache_is_bounded_and_returned_completed_reports_cannot_mutate_it():
    clock = FakeClock()
    cache = clock.cache(max_entries=2)
    keys = [("http://hospital.test", f"transit-{n}", EMS_ID) for n in range(3)]
    report = {"details": {"remote_status": "completed", "patient_id": "synthetic-patient"}}
    for key in keys:
        cache.remember_completed(key, report)
    assert cache.completed(keys[0]) is None
    returned = cache.completed(keys[1])
    returned["details"]["patient_id"] = "changed"
    assert cache.completed(keys[1])["details"]["patient_id"] == "synthetic-patient"
    clock.advance(300)
    assert cache.completed(keys[1]) is None


def test_endpoint_cache_eviction_prevents_unbounded_history():
    clock, calls = FakeClock(), Counter()
    cache = clock.cache(max_entries=2)
    def handler(request):
        calls[str(request.url)] += 1
        return httpx.Response(200, json={"status": "ok"})
    async def scenario(client):
        for identity in range(3):
            await cache.get(client, f"http://command.test/{identity}", 60)
        await cache.get(client, "http://command.test/2", 60)
        assert calls["http://command.test/2"] == 1
        await cache.get(client, "http://command.test/0", 60)
        assert calls["http://command.test/0"] == 2
    run_with_client(handler, scenario)


def test_malformed_capacity_is_negative_cached_independently_of_fleet():
    clock, calls = FakeClock(), Counter()
    cache = clock.cache()
    normal = simulation_handler(calls)
    def handler(request):
        if request.url.path == "/api/departments":
            calls[str(request.url)] += 1
            return httpx.Response(200, json={"items": None})
        return normal(request)
    async def scenario(client):
        for _ in range(2):
            rows = await bridge.connection_status(client, cache=cache)
            assert rows[1]["reachable"] is True
            assert rows[1]["capacity_error"] == "Hospital response omitted department records"
            assert rows[1]["ems"]["fleet_size"] == 8
            assert rows[1]["endpoint_observations"]["departments"]["fresh"] is False
        assert calls["http://hospital.test/api/departments"] == 1
        assert calls["http://hospital.test/api/ems"] == 1
    run_with_client(handler, scenario)


@pytest.mark.parametrize("bad_assignment", [{}, {"id": "x", "agency": "ems"}, {"id": "x", "status": "done"}])
def test_malformed_assignments_cannot_be_positive_terminal_evidence(bad_assignment):
    calls = Counter()
    def command():
        return command_body("resolved", assignments=[bad_assignment])
    with pytest.raises(bridge.BridgeError, match="incomplete assignment"):
        run_with_client(simulation_handler(calls, command=command),
                        lambda client: bridge.observe_incident(COMMAND_ID, "transit-1", client))
