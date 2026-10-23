# Transit integration contract

All events are synthetic. API base: `http://127.0.0.1:8825`. OpenAPI `/docs` is authoritative for field validation. Do not send real emergency requests or person data.

## Transit → 911

`POST /api/scenarios` accepts `kind`, `place_id`, integer `casualties` from 0 through 30 and optional `route_id`. It requires the transit control credential. Valid kinds are `collision`, `medical`, `crowd_surge`, `signal_failure`, `storm`. Use place and route IDs returned by `/api/city` and `/api/routes`.

The incident and its 911 outbox entry commit in one transaction. The worker sends the saved envelope to the server-configured `COMMAND_API_URL` at `/api/integrations/calls` using `X-Dispatch-Token`. Source is `northstar_transit`; external identity is the transit incident ID, which includes a unique run ID. No outbound destination can be selected through an API request or MCP tool.

The current 911 contract is schema `1.0`, synthetic `true`, a stable source/external_id pair, and a `call` containing kind, canonical district, priority, summary, address, alarm channel and a red/yellow/green/black casualty count. Transit uses an explicitly authored triage split, not a clinical inference. The bridge inspects the intake request schema in 911's OpenAPI: new envelopes include a validated canonical `location` only when the observed schema supports it. Unknown or older capabilities produce district/address compatibility envelopes. Capability updates never rewrite an in-flight body or its hash.

HTTP 201 and exact HTTP 200 duplicate receipts acknowledge intake. Network errors, 408/425/429 and 5xx retry with the same frozen body. Other failures block. A worker lease is recoverable after a crash; remote uniqueness prevents double creation after an uncertain response. Inspect `/api/integrations/status`; `POST /api/integrations/outbox/{incident_id}/retry` re-enables a blocked delivery without changing its identity or body.

## Reports into Transit

`POST /api/integrations/reports` requires `X-Transit-Token: <TRANSIT_REPORT_TOKEN>`. A complete example:

```json
{
  "schema_version": "1.0",
  "synthetic": true,
  "source": "hospital",
  "external_id": "hospital-ems-incident-123-arrived-v1",
  "incident_id": "transit-<run-uuid>-000001",
  "kind": "incident_status",
  "status": "transporting",
  "source_sim_time": "2026-07-18T09:14:00-03:00",
  "details": {
    "scope": "ems_journey",
    "remote_incident_id": "<hospital-ems-id>",
    "remote_status": "arrived_hospital",
    "patient_id": "<synthetic-patient-id>",
    "encounter_id": "<synthetic-encounter-id>"
  }
}
```

`source` is 1–80 alphanumeric, underscore or hyphen characters. `external_id` is 1–160 characters; make it stable for one immutable observation. `incident_id` is the transit ID, not the remote 911/EMS ID. Incident reports require it. `kind` is `incident_status`, `capacity` or `telemetry`; capacity/telemetry may omit an incident. `status` is `dispatching`, `on_scene`, `transporting`, `resolved` or `observed`. Include a timezone in `source_sim_time`, or use null if the source clock is unavailable. `details` is an object limited to 64 KiB. Unknown top-level fields are rejected.

New reports return **201**. Replaying the same normalized body and source/external identity returns **200**, `duplicate: true`. Reusing that identity with a different body returns **409**. Unknown incident references return **404**. Use a new external ID for each changed observation. Feedback cannot regress the operational status. Hospital clinical completion is stored, but overall closure of a dispatched transit incident requires a 911 resolution report. Other agency states remain visible without taking ownership of the city incident lifecycle.

Each persisted report separately records source simulation time, transit receipt simulation time and UTC wall observation time. Never subtract timestamps from different simulator clocks to infer travel time. Report histories remain available after a dashboard date jump; the corresponding old simulation run is archived and receives subsequent feedback.

The included bridge polls 911 incident details and linked hospital EMS incidents. It records changes through the same internal report service as the HTTP endpoint. Deduplication ignores advancing source clocks unless operational state changes. It does not dispatch directly to hospital, create local ambulance fleets or advance a paused source simulator. The hospital connection also observes department capacity and EMS availability. An unavailable source is reported as unavailable, never as a completed response.

### Observation scheduling

Dispatch delivery and response observation have separate schedules. The bridge checks for new outbound deliveries every five seconds. Feedback uses a persistent per-incident schedule and recoverable leases, with at most four due incidents claimed per cycle. Active observations start at 30 seconds and ease to 60, 90 and 120 seconds when their state remains unchanged; paused sources use 120 seconds. Failed observations back off from 30 seconds to a maximum of 15 minutes. New work receives priority while an older due slot prevents historical records from being starved.

A resolved 911 incident enters a 15-minute wall-time grace window only when its assignment list is known, every linked EMS journey has been positively observed completed, and there are no unresolved observation errors or pending EMS assignments. It is checked every 120 seconds during grace to discover late assignments. Changes restart reconciliation; after stable completion it is retired from polling. A completed historical record can use its saved completion evidence to establish elapsed grace, but must pass a fresh confirmation before retirement. Failures, a paused source and missing journey data never fabricate completion. Retirement stops automatic reads; it does not remove history or reject later reports.

A new authenticated report wakes reconciliation for its incident, including an archived run. An exact duplicate report does not restart it. `POST /api/integrations/outbox/{incident_id}/refresh`, with a control credential, explicitly queues a feedback check for a delivered incident. It does not resend the emergency call. In-flight observation leases and callback generations prevent an older poll from overwriting this wake-up.

The worker shares cached upstream reads: health every 30 seconds, hospital departments and fleet every 60 seconds, and 911 OpenAPI capabilities every hour. Failed shared reads use 30–300-second backoff. Completed EMS observations may be reused for up to five minutes; an explicit wake-up requests revalidation. Connection details expose actual endpoint observation times and cache freshness separately from Transit’s snapshot time. Capability caching never changes a previously saved dispatch body.

The one-second Transit clock remains local simulation work. It does not poll every incident each tick. The independent environment adapter reuses its observation for up to ten seconds, subject to the source’s expiry. Browser dashboard reads use five seconds on the visible map, ten seconds on other views, and 15 seconds while paused; hidden tabs stop scheduled reads. Integration details refresh only while their views are visible, every 30 seconds. Browser reads use stored Transit observations and never trigger upstream health or incident requests.

## Read access

`GET /api/integrations/reports` supports `source`, `incident_id`, `limit` (1–200) and `offset`. `GET /api/integrations/status` returns the newest 50 outbox entries, aggregate delivery counts, newest 30 reports and connection observations. Its `polling` field includes aggregate schedule states, work awaiting scheduling and the configured policy; each outbox item includes its feedback schedule, next check and last error. These diagnostics are read-only. Archived runs are paginated at `/api/runs`. All read endpoints and MCP tools may observe a later clock on subsequent requests; `/api/dashboard` is one consistent snapshot.

MCP URL: `http://127.0.0.1:8826/mcp`. MCP is intentionally read-only and carries no control, ingestion or upstream credentials. Use the authenticated REST endpoints for producer writes.

## Facility utilities and road traffic

`GET /api/infrastructure` returns one coherent utility snapshot: installation/observation clocks, summary, facility readings, elevator assets, cumulative meters, disruption history and recent load history. `GET /api/infrastructure/facilities/{facility_id}` narrows the response to one facility. IDs are catalog place IDs, such as `northstar_airport`; discover supported facilities through the summary endpoint. Explicit suffixes identify kW, kWh, Mbps, GB, m³/h and m³. These are authored synthetic readings, not measurements of real facilities. Usage counters begin at `installed_at`, including when upgrading an existing run.

`POST /api/infrastructure/disruptions` requires a **control** credential or local dashboard session:

```json
{
  "facility_id": "northstar_airport",
  "utility": "internet",
  "duration_minutes": 30,
  "severity": 1.0
}
```

Utilities are `power`, `elevators`, `internet`, `water` and `sewer`. Duration is an integer from 1–1440 simulated minutes; severity is a numeric unavailable fraction from 0.01–1.0. The response contains `disruption` and `run_id`. An unknown facility returns 404; invalid inputs or overlapping active disruptions to the same facility/utility return 422. `POST /api/infrastructure/disruptions/{id}/restore` restores service early and is idempotent. Automatic restoration follows the transit simulation clock, so pausing also pauses outage duration. Routine utility scenarios do not create 911 calls; emergency scenarios continue through the existing incident/outbox contract.

`GET /api/road-traffic?limit=100&offset=0` includes demand and congestion summaries with paginated vehicle cohorts. `limit` is 1–500. `total` counts visible cohort marks, not represented people or vehicle totals. Read each cohort's representation weights; do not add this aggregate road layer to individual traveler counts. `/api/dashboard` contains both layers at the same clock.

The MCP tools `get_transit_infrastructure`, `get_transit_facility_utilities` and `get_road_traffic` expose these reads without mutation credentials. Other simulators can continue submitting infrastructure-related `telemetry` or `capacity` reports through `/api/integrations/reports`, with scoped report authentication, immutable external identities and source clocks. Those observations are stored as reports; they do not silently override the transit utility model or cause an outage. Use explicit control endpoints when a scenario is intended to change simulated service.

## Registered office commuters

The parallel Office integration adds `POST /api/integrations/workplaces`, authenticated with the scoped **report** token. Send at most 500 workers per chunk with a stable source/external identity, `source: northstar_office`, `schema_version: 1.0`, `synthetic: true`, a zoned `source_sim_time`, a workplace definition and worker schedules. Exact replay is idempotent; changed bodies under the same identity conflict. Canonical residential anchors and allowed modes are exposed at `/api/commutes/catalog`; OpenAPI defines the complete registration schema. Workplace coordinates are immutable under the same ID. Registration extends the city map without changing atlas anchors.

Rosters are interpreted in **Transit local time**. Source timestamps remain recorded; registration does not synchronize clocks or create past outbound journeys. Actual individual people make outbound and return journeys on the existing graph, including capacity-limited public transit when a suitable route exists and explicitly reported car fallbacks otherwise. Return trips require an observed outbound arrival. Deferred departures and missed shifts are reported when the active-person cap prevents materialization. Date changes schedule future departures in the new run; they do not replay unobserved travel.

`GET /api/workplaces` and `GET /api/commutes` expose observations, stable worker/workplace/business identities, recent completed legs, and bounded contact exposure. MCP adds `get_office_workplaces` and `list_office_commutes`. Exposure is a synthetic contact proxy, not an infection diagnosis. Office occupancy remains the Office simulator's independently clocked schedule until an explicit shared-clock policy is introduced. A cached roster and departure queue avoid scanning all registered workers every transit step.
