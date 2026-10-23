# Northstar Transit

A persistent mass-transit and smart-city simulator for the fictional Northstar Region. All travelers, schedules, incidents, sensor readings and events are synthetic.

**Dashboard:** http://127.0.0.1:8825 · **REST/OpenAPI:** http://127.0.0.1:8825/docs · **MCP:** http://127.0.0.1:8826/mcp

The live city has 1,760 building footprints, more than 1,100 road links, 68 destinations, one subway line and connecting bus, rail and ferry services. Individual travelers walk, cycle, take taxis, queue and board capacity-limited vehicles. Airport, rail and seasonal cruise arrivals introduce additional people. Cruise calls run June–September, with extra Saturday calls in July and August. Twenty-eight annual events change travel demand around their venues.

One dot is one modeled traveler in a bounded sample, **not one resident out of the entire 200,000-person region**. Vehicle occupancy, queues, completed journeys, suppressed demand and sensor readings come from the same persisted simulation. The authored demand/capacity assumptions are documented in [docs/MODEL.md](docs/MODEL.md); this is an operational sandbox, not a calibrated forecast or real-time city feed.

Road traffic adds weekday work commutes, school-term journeys, homebound afternoon peaks and weekend travel on the existing streets. Road marks represent explicitly weighted vehicle cohorts, separate from individual passenger dots; the dashboard reports both without combining their counts. See [docs/COMMUTING.md](docs/COMMUTING.md) for schedules, representation and limits.

The **Utilities** dashboard tracks transit-facility electricity, elevator queues/trips, internet bandwidth, water and sewer. Loads respond to traveler activity and equipment operations, with capacities, cumulative meters, backup reserves, outages and restoration controls. Existing runs retain their clocks and identities; meters start when the feature is installed. See [docs/INFRASTRUCTURE.md](docs/INFRASTRUCTURE.md) for coefficients and conservation rules.

## Start and operate

```bash
cd /home/vince/canxp-simulator-transit
python3 scripts/setup.py
docker compose up -d --build
docker compose ps
```

`setup.py` creates a private ignored `.env` once, and copies only the scoped 911 intake credential from the existing local 911 configuration when available. Credentials are never printed. Existing Northstar Docker networks `canxp-911_default` and `canxp-hospital_default` must exist. Compose joins the worker to those networks; it does not redeploy or change sibling services.

The API, worker and MCP run independently. PostgreSQL retains the simulation, current clock, archived runs, immutable dispatch envelopes and integration reports across restarts. Ports bind to loopback. `docker compose down` preserves the volume; **do not use `down -v` unless you deliberately want to erase all transit history**.

The default demo starts on **18 July 2026 at 08:00 Atlantic time**, with a flight at 08:00, cruise call at 08:02 and train at 08:04. Its initial target rate is 60 simulated seconds per wall second. Speed is a target; on an overloaded worker it runs slower rather than silently skipping physics. Pause, speed, date, mode visibility, zoom, place inspection and scenario controls are in the dashboard. Changing date starts a new run at 08:00 and archives the previous state; pending dispatches and old-run feedback remain active and inspectable through the API.

The dashboard obtains a temporary token from an exact same-origin local session endpoint. It never receives the administrator, report-ingest or 911 credential. API clients use separate scoped tokens from `.env` in `X-Transit-Token`:

- `TRANSIT_ADMIN_TOKEN`: clock controls, new runs, incident scenarios and delivery retry.
- `TRANSIT_REPORT_TOKEN`: `POST /api/integrations/reports` only.
- `COMMAND_DISPATCH_TOKEN`: used only by the worker to send synthetic incidents to 911.

## Emergency and simulator connections

Major incidents enter the existing **911** intake. 911 continues to own police/fire/EMS dispatch, avoiding duplicate ambulances. Transit has a durable transactional outbox with immutable bodies, stable idempotency identities, leases and exponential retry. Transient outages retry; invalid/auth/conflicting requests remain visibly blocked for review. Routine sensor noise and zero-casualty signal failures do not generate emergency dispatches.

The bridge observes real 911 assignments and linked hospital EMS journeys and records those observations back into transit. Its read-only hospital connection also exposes capacity, queues and fleet availability. A paused source simulator is labeled paused and remains paused. Accepted dispatch, responder arrival, hospital arrival and incident resolution are distinct states. Only 911 closes a dispatched city incident; a completed hospital journey does not close the entire multi-agency response.

Other simulators can post authenticated, idempotent incident status, capacity or telemetry reports. See [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) for the exact contract and examples. Existing 911/Hospital installations do not push callbacks themselves; the included polling bridge supplies those reports without changing their code.

Feedback polling is scheduled per incident: active checks start at 30 seconds, paused sources slow down, and errors back off. Completed incidents stop polling after a limited window for late EMS updates; a new report or authenticated refresh can reopen reconciliation. Health, hospital capacity and OpenAPI discovery are cached separately. The dashboard suspends background-tab polling and loads integration details only when needed. `/api/integrations/status` exposes the current observation schedules and policy.

## API and MCP

Read endpoints: `/api/health`, `/api/world`, `/api/city`, `/api/spatial/layers`, `/api/spatial/route`, `/api/dashboard`, `/api/routes`, `/api/events`, `/api/people`, `/api/vehicles`, `/api/road-traffic`, `/api/arrivals`, `/api/sensors`, `/api/infrastructure`, `/api/infrastructure/facilities/{facility_id}`, `/api/incidents`, `/api/model`, `/api/runs`, `/api/integrations/status`, `/api/integrations/reports`.

The read-only MCP service uses the official Python SDK and Streamable HTTP. Connect an MCP client to `http://127.0.0.1:8826/mcp`. Its tools cover status, dashboard, model, world, network, routes, events, arrivals, sensors, infrastructure, facility utilities, road traffic, incidents, paginated travelers and vehicles, integrations, reports and registered office commutes. The MCP process has no database or mutation credentials. Host/origin protections remain enabled. If exposing a service beyond localhost, deliberately design authentication and a trusted proxy first; changing the bind alone is not a deployment configuration.

The dashboard and Police now consume the same immutable detailed basemap, served locally at `/api/basemap`. Read `/api/basemap/metadata` for the shared version/hash and `/api/basemap/features` for bounded feature pagination; read-only MCP exposes `get_basemap_metadata` and `list_basemap_features`. Full-region coverage includes both rural districts. Live workplace and Transit operational overlays remain separate, and existing routes, clocks and histories are preserved. See [docs/BASEMAP.md](docs/BASEMAP.md) for publication and routing details.

Local coordinates are `northstar-local-m-v1`, in metres, x east / y north. They are **not GPS**. Canonical airport, harbour, hospital, rail and district anchors remain identical to the shared atlas. Transit streets/buildings/venues are an additive overlay, not a rewrite of the sibling maps. The bridge checks the 911 intake's OpenAPI schema: new dispatches include validated precise locations when supported, and use district/address compatibility otherwise. Every queued body remains frozen even if 911 upgrades while it is waiting.

## Development and verification

Use Python 3.12+:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

Tests use isolated SQLite databases and mock remote HTTP; production uses PostgreSQL. Start API and worker with the same `DATABASE_URL` for local development. Launch only **one clock worker** per database. Simulation steps, controls and reports serialize on the persistent state row. Do not run a local worker against production while the Compose worker is active.

```bash
DATABASE_URL=sqlite:///./transit-dev.db .venv/bin/uvicorn app.main:app --port 8825
DATABASE_URL=sqlite:///./transit-dev.db .venv/bin/python -m app.worker
.venv/bin/python -m app.mcp_server
```

For backups, stop only this simulator's worker/API/MCP, leave its database running, then create a private PostgreSQL archive outside Git. The following keeps database credentials inside the container:

```bash
docker compose stop worker api mcp
umask 077
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > /path/outside/git/transit.dump
docker compose start api worker mcp
```

The shared repository's `scripts/world_admin.py check` includes Transit. Its historical medical migrations and five-service backup routines deliberately do not operate on the new transit schema.

## Shared regional time

The deployed simulator can follow the coordinated Northstar clock. See [SHARED_CLOCK.md](docs/SHARED_CLOCK.md) for configuration, controls, historical timestamp provenance and standalone-mode behavior. In shared mode, this supersedes older independent-clock descriptions below/above.
