"""Synthetic simulator transport helpers; the caller owns persistence and retries.

Requests are frozen by the transit outbox before calling this module. Older 911
intakes receive district/address only. New bodies may include validated metre
locations after a read-only OpenAPI capability check; retries are never rebuilt.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import asyncio
import hashlib
import json
import os
import time
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import UUID
from weakref import WeakKeyDictionary

import httpx

DISTRICTS = {"central", "north", "east", "south", "west", "coastal", "rural_north", "rural_south"}
KINDS = {"collision": "road_collision", "crash": "road_collision", "medical": "medical",
         "crowd_surge": "public_event", "signal_failure": "power_outage", "storm": "storm_damage"}
COMMAND_STATES = {"call_taking": "dispatching", "ready": "dispatching", "dispatched": "dispatching",
                  "on_scene": "on_scene", "resolved": "resolved"}
EMS_STATES = {"queued": "dispatching", "en_route": "dispatching", "on_scene": "on_scene",
              "transporting": "transporting", "arrived_hospital": "transporting",
              "returning": "transporting", "completed": "resolved"}
TIMEOUT = httpx.Timeout(8.0, connect=3.0)
HEALTH_TTL = 30.0
CAPABILITY_TTL = 3600.0
CAPACITY_TTL = 60.0
COMPLETED_EMS_TTL = 300.0


class BridgeError(Exception):
    """Sanitized adapter failure, classified for the durable outbox."""

    def __init__(self, message: str, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.observation: dict | None = None


class BridgeCache:
    """Bounded, process-local read cache with independent endpoint retry clocks.

    Successful observations expire on a monotonic clock. Failed refreshes retain
    only the previous success timestamp, never a stale response presented as
    current. Clocks are injectable so expiry/backoff tests need no real sleeps.
    This cache never contains dispatch credentials or outbound request bodies.
    """

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic,
                 wall_now: Callable[[], datetime] | None = None, max_entries: int = 4096):
        self.monotonic = monotonic
        self.wall_now = wall_now or (lambda: datetime.now(timezone.utc))
        self.max_entries = max(1, int(max_entries))
        self._entries: OrderedDict[str, dict] = OrderedDict()
        self._completed: OrderedDict[tuple[str, str, str], tuple[float, dict]] = OrderedDict()
        # Clients in separate test/event loops must not share loop-bound locks.
        self._locks: WeakKeyDictionary = WeakKeyDictionary()

    def _stamp(self) -> str:
        return self.wall_now().isoformat().replace("+00:00", "Z")

    def _prune(self, mapping: OrderedDict) -> None:
        while len(mapping) > self.max_entries:
            mapping.popitem(last=False)

    def _metadata(self, entry: dict, ttl: float, *, cache_hit: bool) -> dict:
        now = self.monotonic()
        observed = entry.get("observed_mono")
        fresh = entry.get("body") is not None and not entry.get("error") and observed is not None and now - observed < ttl
        return {"observed_at": entry.get("observed_at"), "checked_at": entry.get("checked_at"),
                "age_seconds": round(max(0.0, now - observed), 3) if observed is not None else None,
                "ttl_seconds": ttl, "fresh": fresh, "cache_hit": cache_hit,
                "consecutive_failures": entry.get("failures", 0),
                "retry_after_seconds": round(max(0.0, entry.get("retry_at", 0.0) - now), 3)}

    def _failure(self, entry: dict, ttl: float, *, cache_hit: bool) -> BridgeError:
        message, retryable, status = entry["error"]
        error = BridgeError(message, retryable=retryable, status=status)
        error.observation = self._metadata(entry, ttl, cache_hit=cache_hit)
        return error

    async def get(self, client: httpx.AsyncClient, url: str, ttl: float, *,
                  force_refresh: bool = False, validate: Callable[[dict], None] | None = None) -> tuple[dict, dict]:
        """Reuse fresh reads and coalesce concurrent requests for the same URL.

        A zero success TTL is used for active incident reads: every scheduled
        observation gets fresh lifecycle data, but unavailable endpoints still
        back off. Explicit wakeups may bypass that negative cache.
        """
        loop = asyncio.get_running_loop()
        locks = self._locks.setdefault(loop, {})
        lock = locks.setdefault(url, asyncio.Lock())
        try:
            async with lock:
                entry = self._entries.get(url, {})
                now = self.monotonic()
                if entry and not force_refresh:
                    self._entries.move_to_end(url)
                    if entry.get("error") and now < entry.get("retry_at", 0):
                        raise self._failure(entry, ttl, cache_hit=True)
                    if entry.get("body") is not None and now - entry["observed_mono"] < ttl:
                        return deepcopy(entry["body"]), self._metadata(entry, ttl, cache_hit=True)
                try:
                    body = await _request(client, "GET", url)
                    if validate is not None:
                        validate(body)
                except BridgeError as exc:
                    failures = min(entry.get("failures", 0) + 1, 10)
                    entry = {"body": None, "observed_at": entry.get("observed_at"),
                             "observed_mono": entry.get("observed_mono"), "checked_at": self._stamp(),
                             "failures": failures, "retry_at": self.monotonic() + min(300.0, 30.0 * 2 ** (failures - 1)),
                             "error": (str(exc), exc.retryable, exc.status)}
                    self._entries[url] = entry
                    self._prune(self._entries)
                    raise self._failure(entry, ttl, cache_hit=False) from None
                entry = {"body": deepcopy(body), "observed_at": self._stamp(), "observed_mono": self.monotonic(),
                         "failures": 0, "retry_at": 0.0, "error": None}
                entry["checked_at"] = entry["observed_at"]
                self._entries[url] = entry
                self._prune(self._entries)
                return body, self._metadata(entry, ttl, cache_hit=False)
        finally:
            # Waiters hold their lock reference; remove only once none remain.
            if not lock.locked() and not getattr(lock, "_waiters", None):
                locks.pop(url, None)

    def completed(self, key: tuple[str, str, str], *, force_refresh: bool = False) -> dict | None:
        previous = self._completed.get(key)
        if previous is None:
            return None
        observed, report = previous
        if force_refresh or self.monotonic() - observed >= COMPLETED_EMS_TTL:
            self._completed.pop(key, None)
            return None
        self._completed.move_to_end(key)
        return deepcopy(report)

    def remember_completed(self, key: tuple[str, str, str], report: dict) -> None:
        if report.get("details", {}).get("remote_status") != "completed":
            return
        now = self.monotonic()
        for old_key, (observed, _) in list(self._completed.items()):
            if now - observed >= COMPLETED_EMS_TTL:
                self._completed.pop(old_key, None)
        self._completed[key] = (now, deepcopy(report))
        self._completed.move_to_end(key)
        self._prune(self._completed)


_DEFAULT_CACHE = BridgeCache()


def _serious(incident: dict) -> bool:
    severity = incident.get("severity", "")
    return (isinstance(severity, (int, float)) and not isinstance(severity, bool) and severity >= 3) or str(severity).lower() in {
        "major", "critical", "severe", "high", "emergency", "mass_casualty",
    }


def should_dispatch(incident: dict) -> bool:
    """Routine congestion/sensor alarms do not create emergency calls."""
    kind = incident.get("type")
    if kind not in KINDS:
        return False
    casualties = incident.get("casualties", 0)
    injured = isinstance(casualties, int) and not isinstance(casualties, bool) and casualties > 0
    if kind == "signal_failure":
        return injured
    return injured or kind == "medical" or _serious(incident)


def build_envelope(incident: dict, supports_location: bool = False) -> dict:
    """Build a strict 911 request using capabilities observed before enqueue."""
    kind = incident.get("type")
    if kind not in KINDS:
        raise ValueError("Unsupported transit incident type")
    if not should_dispatch(incident):
        raise ValueError("This transit disruption does not require emergency dispatch")
    identity = incident.get("id")
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 120:
        raise ValueError("Incident id must contain 1–120 characters")
    district = incident.get("district_id")
    if district not in DISTRICTS:
        raise ValueError("Unknown Northstar district")
    casualties = incident.get("casualties", 0)
    if isinstance(casualties, bool) or not isinstance(casualties, int) or not 0 <= casualties <= 30:
        raise ValueError("Casualties must be an integer from 0 through 30")
    serious = _serious(incident)
    # Authored exercise triage split; this is not a clinical assessment.
    red = max(1, casualties // 3) if serious and casualties else 0
    title = str(incident.get("title") or kind.replace("_", " ")).strip()
    route = incident.get("route_id")
    summary = f"Synthetic Northstar transit incident: {title}."
    if route:
        summary += f" Transit route: {route}."
    summary += f" Reported casualties: {casualties}; authored exercise triage."
    location = incident.get("location")
    address = incident.get("address") or (location if isinstance(location, str) else None)
    if not address:
        address = str(incident.get("place_id") or district).replace("_", " ").title() + ", Northstar (synthetic)"
    envelope = {
        "schema_version": "1.0", "synthetic": True, "source": "northstar_transit", "external_id": identity,
        "call": {"kind": KINDS[kind], "district_id": district, "priority": 1 if serious else 2,
                 "summary": summary[:1000], "address": str(address)[:240], "channel": "alarm",
                 "casualties": {"red": red, "yellow": casualties - red, "green": 0, "black": 0}},
    }
    if supports_location is True:
        from .spatial_contract import validate_location
        envelope["call"]["location"] = validate_location(location, district_id=district)
    return envelope


def _supports_location(document: dict) -> bool:
    """Inspect the actual call-ingestion schema, following only local JSON refs."""
    def resolve(schema):
        visited = set()
        while isinstance(schema, dict) and "$ref" in schema:
            reference = schema["$ref"]
            if not isinstance(reference, str) or not reference.startswith("#/") or reference in visited or len(visited) >= 8:
                return {}
            visited.add(reference)
            value = document
            for component in reference[2:].split("/"):
                component = component.replace("~1", "/").replace("~0", "~")
                if not isinstance(value, dict):
                    return {}
                value = value.get(component)
            schema = value
        return schema if isinstance(schema, dict) else {}

    try:
        request = resolve(document["paths"]["/api/integrations/calls"]["post"]["requestBody"])
        envelope = resolve(request["content"]["application/json"]["schema"])
        call = resolve(envelope["properties"]["call"])
        location = resolve(call["properties"]["location"])
        variants = location.get("anyOf", [location])
        for variant in variants:
            properties = resolve(variant).get("properties", {})
            if (isinstance(properties, dict) and "x" in properties and "y" in properties
                and properties.get("crs", {}).get("const") == "northstar-local-m-v1"
                and properties.get("units", {}).get("const") == "m"):
                return True
    except (KeyError, TypeError, AttributeError):
        pass
    return False


def _base(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        raise BridgeError("Simulator API URL configuration is invalid") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BridgeError("Simulator API URL configuration is invalid")
    return value


def _token() -> str:
    return os.getenv("COMMAND_DISPATCH_TOKEN", "").strip() or os.getenv("TRANSIT_DISPATCH_TOKEN", "").strip()


def _uuid(value: Any, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise BridgeError(f"Simulator returned invalid {label}", retryable=True) from None


@asynccontextmanager
async def _client(client: httpx.AsyncClient | None):
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, trust_env=False) as owned:
            yield owned


async def _request(client: httpx.AsyncClient, method: str, url: str, *, payload=None, token=None, accepted=(200,)) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Dispatch-Token"] = token
    try:
        response = await client.request(method, url, json=payload, headers=headers,
                                        timeout=TIMEOUT, follow_redirects=False)
    except (httpx.HTTPError, httpx.InvalidURL, OSError):
        raise BridgeError("Simulator connection unavailable", retryable=True) from None
    if response.status_code not in accepted:
        status = response.status_code
        raise BridgeError(f"Simulator returned HTTP {status}", retryable=status >= 500 or status in {408, 425, 429}, status=status)
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError):
        raise BridgeError("Simulator returned invalid JSON", retryable=True) from None
    if not isinstance(body, dict):
        raise BridgeError("Simulator returned an invalid response", retryable=True)
    return body


async def send_envelope(payload: dict, client: httpx.AsyncClient | None = None) -> dict:
    """Deliver an already-frozen body; never rebuild it or change its identity."""
    token = _token()
    if not token:
        raise BridgeError("911 dispatch credential is not configured")
    if not isinstance(payload, dict) or payload.get("synthetic") is not True:
        raise BridgeError("Only explicitly synthetic dispatch payloads are accepted")
    async with _client(client) as http:
        body = await _request(http, "POST", _base("COMMAND_API_URL", "http://127.0.0.1:8805") + "/api/integrations/calls",
                              payload=payload, token=token, accepted=(200, 201))
    call = body.get("call")
    if not isinstance(call, dict) or not isinstance(body.get("duplicate"), bool):
        raise BridgeError("911 response omitted its call receipt", retryable=True)
    return {**body, "remote_call_id": _uuid(call.get("id"), "call identity"),
            "remote_incident_id": _uuid(call.get("incident_id"), "incident identity")}


def _report(source: str, identity: str, transit_id: str, status: str, source_time: Any, details: dict) -> dict:
    # Clock advancement by itself is not a new observed lifecycle event.
    fingerprint = json.dumps({"identity": identity, "transit_incident_id": transit_id, "status": status, "details": details}, sort_keys=True, separators=(",", ":"))
    external_id = "transit-observation-" + hashlib.sha256(fingerprint.encode()).hexdigest()
    return {"source": source, "external_id": external_id, "incident_id": transit_id, "status": status,
            "source_sim_time": source_time if isinstance(source_time, str) else None, "details": details}


def _assignment_summary(item: dict) -> dict:
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    return {"assignment_id": item.get("id"), "agency": item.get("agency"), "status": item.get("status"),
            "remote_status": item.get("remote_status"), "remote_id": item.get("remote_id"),
            "external_id": item.get("external_id"), "hospital_patient_id": details.get("hospital_patient_id"),
            "hospital_encounter_id": details.get("hospital_encounter_id"),
            "last_poll_error": details.get("last_poll_error") is not None}


def _validate_health(body: dict) -> None:
    if not (isinstance(body.get("status"), str) or isinstance(body.get("paused"), bool)
            or isinstance(body.get("simulation"), dict)):
        raise BridgeError("Simulator response omitted health records", retryable=True)


def _paused(health: dict) -> bool | None:
    sim = health.get("simulation") if isinstance(health.get("simulation"), dict) else {}
    value = sim.get("paused", health.get("paused"))
    return value if isinstance(value, bool) else None


def _validate_command(body: dict, remote_id: str) -> None:
    incident = body.get("incident")
    if not isinstance(incident, dict) or _uuid(incident.get("id"), "incident identity") != remote_id:
        raise BridgeError("911 observation changed incident identity", retryable=True)
    if not isinstance(incident.get("status"), str) or incident["status"] not in COMMAND_STATES:
        raise BridgeError("911 returned an unknown incident status", retryable=True)
    assignments = body.get("assignments")
    if not isinstance(assignments, list) or not all(isinstance(item, dict) for item in assignments):
        raise BridgeError("911 observation omitted assignment records", retryable=True)
    if not all(all(isinstance(item.get(key), str) and item[key] for key in ("id", "agency", "status")) for item in assignments):
        raise BridgeError("911 observation returned incomplete assignment records", retryable=True)


def _validate_ems(body: dict, ems_id: str) -> None:
    ems = body.get("incident")
    if not isinstance(ems, dict) or _uuid(ems.get("id"), "EMS identity") != ems_id:
        raise BridgeError("Hospital observation changed EMS identity", retryable=True)
    if not isinstance(ems.get("status"), str) or ems["status"] not in EMS_STATES:
        raise BridgeError("Hospital returned an unknown EMS status", retryable=True)


def _validate_departments(body: dict) -> None:
    if not isinstance(body.get("items"), list):
        raise BridgeError("Hospital response omitted department records", retryable=True)


def _validate_fleet(body: dict) -> None:
    if not isinstance(body.get("stats"), dict):
        raise BridgeError("Hospital response omitted fleet records", retryable=True)


async def observe_incident(remote_incident_id: str, transit_incident_id: str,
                           client: httpx.AsyncClient | None = None, *,
                           cache: BridgeCache | None = None, force_refresh: bool = False) -> list[dict]:
    """Observe 911 and each linked EMS journey, preserving their independent clocks.

Only a 911 report with status resolved establishes whole-incident resolution.
Hospital reports have scope ems_journey; a completed trip does not settle other
casualties, fire response, police work, or the transit incident as a whole.
Completed journeys are historical snapshots reused for at most five minutes.
Explicit callbacks/manual wakeups bypass those snapshots and negative incident
caches, while leaving the shared health observation TTL intact.
"""
    cache = cache if cache is not None else _DEFAULT_CACHE
    remote_id = _uuid(remote_incident_id, "incident identity")
    command_url = _base("COMMAND_API_URL", "http://127.0.0.1:8805")
    reports = []
    async with _client(client) as http:
        body, _ = await cache.get(http, f"{command_url}/api/incidents/{remote_id}", 0,
                                  force_refresh=force_refresh, validate=lambda data: _validate_command(data, remote_id))
        incident = body["incident"]
        status = COMMAND_STATES[incident["status"]]
        assignments = body["assignments"]
        details = {"synthetic": True, "scope": "command_incident", "remote_incident_id": remote_id,
                   "correlation_id": incident.get("correlation_id"), "remote_status": incident.get("status"),
                   "casualties": incident.get("casualties"), "command_status": incident["status"],
                   "assignments_known": True, "linked_ems_ids": [], "completed_ems_ids": [],
                   "command_paused": None, "hospital_paused": None, "observation_errors": [],
                   "assignments": sorted((_assignment_summary(item) for item in assignments), key=lambda item: str(item["assignment_id"]))}
        try:
            command_health, _ = await cache.get(http, command_url + "/api/health", HEALTH_TTL, validate=_validate_health)
            details["command_paused"] = _paused(command_health)
        except BridgeError as exc:
            details["command_health_unavailable"] = True
            details["observation_errors"].append({"source": "911", "endpoint": "health", "error": str(exc), "retryable": exc.retryable})
        ems_assignments = [item for item in assignments if item.get("agency") == "ems" and item.get("remote_id")]
        if ems_assignments:
            hospital_url = _base("HOSPITAL_API_URL", "http://127.0.0.1:8775")
            hospital_health = {}
            try:
                hospital_health, _ = await cache.get(http, hospital_url + "/api/health", HEALTH_TTL, validate=_validate_health)
                details["hospital_paused"] = _paused(hospital_health)
            except BridgeError as exc:
                details["hospital_health_unavailable"] = True
                details["observation_errors"].append({"source": "hospital", "endpoint": "health", "error": str(exc), "retryable": exc.retryable})
            errors = []
            seen = set()
            for assignment in ems_assignments:
                try:
                    ems_id = _uuid(assignment["remote_id"], "EMS identity")
                    if ems_id in seen:
                        continue
                    seen.add(ems_id)
                    key = (hospital_url, transit_incident_id, ems_id)
                    completed = cache.completed(key, force_refresh=force_refresh)
                    if completed is not None:
                        reports.append(completed)
                        details["completed_ems_ids"].append(ems_id)
                        continue
                    ems_body, _ = await cache.get(http, f"{hospital_url}/api/ems/incidents/{ems_id}", 0,
                                                  force_refresh=force_refresh, validate=lambda data: _validate_ems(data, ems_id))
                    ems = ems_body["incident"]
                    ems_status = EMS_STATES[ems["status"]]
                    patient = ems_body.get("patient") if isinstance(ems_body.get("patient"), dict) else {}
                    encounter = ems_body.get("encounter") if isinstance(ems_body.get("encounter"), dict) else {}
                    unit = ems_body.get("unit") if isinstance(ems_body.get("unit"), dict) else {}
                    hospital_details = {"synthetic": True, "scope": "ems_journey", "remote_incident_id": ems_id,
                                        "command_incident_id": remote_id, "assignment_id": assignment.get("id"),
                                        "remote_status": ems.get("status"), "source_external_id": ems.get("external_id"),
                                        "patient_id": ems.get("patient_id") or patient.get("id"),
                                        "encounter_id": ems.get("encounter_id") or encounter.get("id"),
                                        "encounter_status": encounter.get("status"), "department_id": encounter.get("department_id"),
                                        "unit_id": ems.get("unit_id") or unit.get("id"), "unit_service_id": unit.get("service_id"),
                                        "paused": _paused(hospital_health),
                                        "worker_status": (hospital_health["worker"].get("status") if isinstance(hospital_health.get("worker"), dict)
                                                          else hospital_health.get("worker")),
                                        "arrived_hospital_at": ems.get("arrived_hospital_at"), "completed_at": ems.get("completed_at")}
                    if ems["status"] == "completed":
                        # Clinical/health fields describe this completion observation,
                        # not the patient's later care or a fresh service heartbeat.
                        hospital_details["observation_scope"] = "ems_completion_snapshot"
                    source_time = next((ems.get(key) for key in ("completed_at", "arrived_hospital_at", "transport_started_at", "on_scene_at", "assigned_at", "requested_at") if ems.get(key)), None)
                    report = _report("hospital", ems_id, transit_incident_id, ems_status, source_time, hospital_details)
                    reports.append(report)
                    if ems["status"] == "completed":
                        cache.remember_completed(key, report)
                        details["completed_ems_ids"].append(ems_id)
                except BridgeError as exc:
                    errors.append({"remote_id": str(assignment.get("remote_id")), "error": str(exc), "retryable": exc.retryable})
            details["linked_ems_ids"] = sorted(seen)
            details["completed_ems_ids"].sort()
            if errors:
                details["hospital_observation_errors"] = errors
                details["observation_errors"].extend({"source": "hospital", "endpoint": "ems_incident", **error} for error in errors)
        source_time = incident.get("closed_at") or incident.get("dispatched_at") or incident.get("received_at")
        reports.insert(0, _report("911", remote_id, transit_incident_id, status, source_time, details))
    return reports


async def connection_status(client: httpx.AsyncClient | None = None, *, cache: BridgeCache | None = None) -> list[dict]:
    """Read-only source observations with explicit freshness and retry metadata.

    observed_at is the last successful *health endpoint* observation, never the
    time this cached connection row was assembled. checked_at includes failures.
    An expired health response is not reused if its refresh fails.
    """
    cache = cache if cache is not None else _DEFAULT_CACHE
    result = []
    async with _client(client) as http:
        for source, variable, default in (("911", "COMMAND_API_URL", "http://127.0.0.1:8805"),
                                          ("hospital", "HOSPITAL_API_URL", "http://127.0.0.1:8775")):
            item = {"id": source, "source": source, "reachable": False, "status": "unavailable", "synthetic": True,
                    "observed_at": None, "health_observed_at": None, "checked_at": None, "endpoint_observations": {}}
            if source == "911":
                item["dispatch_configured"] = bool(_token())
                item["supports_location"] = False
            try:
                base = _base(variable, default)
                try:
                    health, observation = await cache.get(http, base + "/api/health", HEALTH_TTL, validate=_validate_health)
                except BridgeError as exc:
                    if exc.observation is not None:
                        item["endpoint_observations"]["health"] = exc.observation
                        item.update(observed_at=exc.observation["observed_at"], health_observed_at=exc.observation["observed_at"],
                                    checked_at=exc.observation["checked_at"])
                    raise
                item["endpoint_observations"]["health"] = observation
                item.update(observed_at=observation["observed_at"], health_observed_at=observation["observed_at"], checked_at=observation["checked_at"])
                sim = health.get("simulation") if isinstance(health.get("simulation"), dict) else {}
                settings = sim.get("settings") if isinstance(sim.get("settings"), dict) else {}
                worker = health.get("worker")
                world = health.get("world") if isinstance(health.get("world"), dict) else {}
                item.update(reachable=True, status=health.get("status", "unknown"),
                            worker_status=worker.get("status") if isinstance(worker, dict) else worker,
                            paused=_paused(health),
                            source_sim_time=sim.get("simulated_at", health.get("simulated_at")),
                            world_version=health.get("world_version") or world.get("world_version") or world.get("version") or settings.get("world_version"),
                            spatial_version=health.get("spatial_version") or world.get("spatial_version") or settings.get("spatial_version"),
                            api_version=health.get("version"), version=health.get("version"))
                if isinstance(health.get("world"), str):
                    item["world"] = health["world"]
                if source == "911":
                    try:
                        specification, observation = await cache.get(http, base + "/openapi.json", CAPABILITY_TTL)
                        item["endpoint_observations"]["openapi"] = observation
                        item["supports_location"] = _supports_location(specification)
                        info = specification.get("info") if isinstance(specification.get("info"), dict) else {}
                        if isinstance(info.get("version"), str):
                            item["api_version"] = info["version"]
                            item["version"] = info["version"]
                    except BridgeError as exc:
                        item["capability_error"] = str(exc)
                        if exc.observation is not None:
                            item["endpoint_observations"]["openapi"] = exc.observation
                if source == "hospital":
                    try:
                        departments, observation = await cache.get(http, base + "/api/departments", CAPACITY_TTL, validate=_validate_departments)
                        item["endpoint_observations"]["departments"] = observation
                        rows = departments["items"]
                        item["departments"] = [{k: d.get(k) for k in ("id", "name", "capacity", "occupied", "waiting", "boarding", "occupancy_pct", "capacity_status")}
                                                for d in rows if isinstance(d, dict)]
                    except BridgeError as exc:
                        item["capacity_error"] = str(exc)
                        if exc.observation is not None:
                            item["endpoint_observations"]["departments"] = exc.observation
                    try:
                        fleet, observation = await cache.get(http, base + "/api/ems", CAPACITY_TTL, validate=_validate_fleet)
                        item["endpoint_observations"]["ems_fleet"] = observation
                        item["ems"] = fleet.get("stats", {})
                    except BridgeError as exc:
                        item["capacity_error"] = str(exc)
                        if exc.observation is not None:
                            item["endpoint_observations"]["ems_fleet"] = exc.observation
            except BridgeError as exc:
                item["error"] = str(exc)
            result.append(item)
    return result
