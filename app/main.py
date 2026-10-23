"""Northstar transit REST API and local operational dashboard."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, func, select

from . import cruise_terminal, db, engine, infrastructure, services, workplaces
from .catalog import CITY, WORLD
from .spatial_contract import attach_spatial_routes
from .basemap import attach_basemap_routes, metadata as basemap_metadata


@asynccontextmanager
async def lifespan(app):
    db.init_db()
    yield


app = FastAPI(title='Northstar Transit Simulator', version='1.1.0', lifespan=lifespan,
    description='Fictional Northstar mobility, smart sensors, seasonal demand and emergency integrations. '
        'All people, incidents and readings are simulated. Coordinates use the Northstar local metre grid.')
attach_spatial_routes(app)
attach_basemap_routes(app)
STATIC = Path(__file__).with_name('static')
app.mount('/static', StaticFiles(directory=STATIC), name='static')


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def require_control(x_transit_token: Annotated[str | None, Header()] = None):
    supplied = x_transit_token or ''
    admin = os.getenv('TRANSIT_ADMIN_TOKEN', '')
    if admin and supplied and hmac.compare_digest(supplied, admin):
        return
    if supplied:
        with db.Session() as session:
            entry = session.get(db.UISession, hash_token(supplied))
            if entry and entry.expires_at.replace(tzinfo=timezone.utc) > db.now():
                return
    raise HTTPException(401, 'A valid X-Transit-Token is required.')


def require_report(x_transit_token: Annotated[str | None, Header()] = None):
    expected = os.getenv('TRANSIT_REPORT_TOKEN', '')
    if not expected or not x_transit_token or not hmac.compare_digest(expected, x_transit_token):
        raise HTTPException(401, 'A scoped report token is required in X-Transit-Token.')


workplaces.attach_routes(app, require_report)


def state_snapshot():
    with db.Session() as session:
        row = session.get(db.Simulation, 1)
        result = engine.snapshot(row.payload)
        result.update(run_id=row.run_id, synthetic=True, world_version=WORLD['version'])
        return result


@app.middleware('http')
async def response_headers(request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['X-Frame-Options'] = 'DENY'
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/', include_in_schema=False)
def index():
    return FileResponse(STATIC / 'index.html', headers={'Cache-Control': 'no-cache'})


@app.post('/api/session', include_in_schema=False)
def local_session(request: Request):
    # Local-only Compose binding plus exact Origin + custom header prevents browser CSRF.
    # Mint a short-lived control token, never reveal integration/admin credentials.
    allowed = os.getenv('TRANSIT_UI_ORIGINS', 'http://127.0.0.1:8825,http://localhost:8825').split(',')
    origin = request.headers.get('origin', '')
    if origin not in allowed or origin != str(request.base_url).rstrip('/') or \
        request.headers.get('x-requested-with') != 'NorthstarDashboard' or \
        request.headers.get('sec-fetch-site', 'same-origin') != 'same-origin':
        raise HTTPException(403, 'Dashboard sessions require an approved same-origin local request.')
    token = secrets.token_urlsafe(32)
    with db.Session.begin() as session:
        session.execute(delete(db.UISession).where(db.UISession.expires_at < db.now()))
        session.add(db.UISession(token_hash=hash_token(token), expires_at=db.now() + timedelta(hours=12)))
    return {'token': token, 'expires_in': 43200}


@app.get('/api/environment', tags=['System'])
def environment_status():
    from app.environment_effects import report
    with db.Session() as session:
        row = session.get(db.Simulation, 1)
        return report(row.payload.get('environment'), 'transit', row.payload.get('paused', False), row.worker_at.isoformat() if row.worker_at else None)


@app.get('/api/health', tags=['System'])
def health():
    with db.Session() as session:
        row = session.get(db.Simulation, 1)
        age = (db.now() - row.worker_at.replace(tzinfo=timezone.utc)).total_seconds() if row.worker_at else None
        return {'status': 'ok', 'database': 'ok', 'synthetic': True, 'world_version': WORLD['version'],
            'worker': 'healthy' if age is not None and age < 30 else 'stale', 'worker_age_seconds': age,
            'paused': row.payload['paused'], 'run_id': row.run_id,
            'simulated_at': datetime.fromtimestamp(row.payload['t'], timezone.utc).isoformat()}


@app.get('/api/world', tags=['Map'])
def world():
    return WORLD


@app.get('/api/city', tags=['Map'])
def city():
    return workplaces.city_overlay()


@app.get('/api/dashboard', tags=['Operations'])
def dashboard():
    return state_snapshot()


@app.get('/api/model', tags=['System'])
def model():
    return {'synthetic': True, 'model_scope': 'Sample of individual synthetic travelers in a fictional 200,000-person region.',
        'dot_definition': 'One displayed dot is one modeled person. Counts are not full regional population estimates.',
        'cruise_months': [6, 7, 8, 9], 'timezone': 'America/Moncton', 'crs': 'northstar-local-m-v1',
        'units': 'm', 'basemap': basemap_metadata(), 'calibration': 'Authored service frequencies, demands, capacities and incident rates; not calibrated to real observations.',
        'road_traffic': {'scope': 'Weighted road vehicle cohorts, separate from individually modeled transit travelers.',
            'endpoint': '/api/road-traffic', 'documentation': '/api/model/documentation/commuting'},
        'infrastructure': {'scope': 'Transit facilities and their authored operational equipment; not all city utilities.',
            'utilities': ['power', 'elevators', 'internet', 'water', 'sewer'],
            'meter_policy': 'Cumulative meters start at installation in each run; no historical usage is backfilled.',
            'endpoint': '/api/infrastructure', 'documentation': '/api/model/documentation/infrastructure'},
        'feedback': 'Source simulator clocks and wall observation times are retained separately. Only 911 resolves an externally dispatched city incident.',
        'source_references': [
            {'title': 'GTFS schedule reference — routes, stops and headways', 'url': 'https://gtfs.org/documentation/schedule/reference/',
             'role': 'Conceptual scheduling vocabulary only; this local grid is not a WGS84 GTFS feed.'},
            {'title': 'MCP Streamable HTTP specification', 'url': 'https://modelcontextprotocol.io/specification/2025-11-25/basic/transports',
             'role': 'Read-only MCP protocol through the official Python SDK.'},
            {'title': 'FHWA travel patterns', 'url': 'https://www.fhwa.dot.gov/policyinformation/pubs/hf/pl11028/chapter4.cfm',
             'role': 'Conceptual morning/evening commute and school peaks; Northstar rates are independently authored.'},
            {'title': 'DOE Building Energy Asset Score', 'url': 'https://www.energy.gov/cmei/buildings/building-energy-asset-score',
             'role': 'Conceptual energy end-use categories; this is not a calibrated building physics model.'}],
        'office_commutes': {'registration':'/api/integrations/workplaces','observations':'/api/commutes',
            'clock_policy':'interpret_recurring_rosters_in_transit_local_time','office_occupancy':'independent_source_schedule',
            'active_person_cap':5000,'max_persisted_workers':50000,'cap_overflow':'explicit_deferred_commute_requests'},
        'documentation': '/api/model/documentation'}


@app.get('/api/model/documentation', tags=['System'])
def model_documentation():
    path = Path(__file__).parents[1] / 'docs' / 'MODEL.md'
    return {'content': path.read_text() if path.exists() else 'Model documentation is being installed.'}


@app.get('/api/model/documentation/{topic}', tags=['System'])
def model_topic(topic: Literal['infrastructure', 'commuting']):
    path = Path(__file__).parents[1] / 'docs' / f'{topic.upper()}.md'
    return {'topic': topic, 'content': path.read_text()}


@app.get('/api/routes', tags=['Operations'])
def routes():
    return {'items': CITY['routes']}


@app.get('/api/events', tags=['Calendar'])
def events(month: int | None = Query(None, ge=1, le=12)):
    return {'items': [e for e in CITY['events'] if month is None or e['month'] == month],
        'recurrence': 'annual', 'timezone': 'America/Moncton', 'cruise_months': [6, 7, 8, 9]}


@app.get('/api/people', tags=['Operations'])
def people(mode: str | None = None, status: str | None = None,
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    snapshot = state_snapshot()
    items = [p for p in snapshot['people'] if (mode is None or p['mode'] == mode) and (status is None or p['status'] == status)]
    return {'items': items[offset:offset+limit], 'total': len(items), 'offset': offset, 'limit': limit,
        'clock': snapshot['clock'], 'run_id': snapshot['run_id']}


@app.get('/api/vehicles', tags=['Operations'])
def vehicles(mode: str | None = None, route_id: str | None = None,
    limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
    snapshot = state_snapshot()
    items = [v for v in snapshot['vehicles'] if (mode is None or v['mode'] == mode)
        and (route_id is None or v['route_id'] == route_id)]
    return {'items': items[offset:offset+limit], 'total': len(items), 'offset': offset, 'limit': limit,
        'clock': snapshot['clock'], 'run_id': snapshot['run_id'], 'gateway_vehicles': snapshot.get('gateway_vehicles', [])}


@app.get('/api/arrivals', tags=['Operations'])
def arrivals():
    snapshot = state_snapshot()
    return {'items': snapshot['arrivals'], 'clock': snapshot['clock']}


@app.get('/api/cruise-terminal', tags=['Operations'])
def cruise_terminal_view():
    """Cruise terminal operations: ships, queues, destinations and return-to-ship rates."""
    with db.Session() as session:
        row = session.get(db.Simulation, 1)
        detail = cruise_terminal.cruise_terminal_detail(row.payload)
        detail['run_id'] = row.run_id
        detail['world_version'] = WORLD['version']
        return detail


class CruiseScenario(Strict):
    berths: int | None = Field(None, ge=1, le=3, strict=True)
    customs_lanes: int | None = Field(None, ge=1, le=12, strict=True)
    security_lanes: int | None = Field(None, ge=1, le=12, strict=True)
    customs_seconds_per_group: int | None = Field(None, ge=10, le=180, strict=True)
    security_seconds_per_group: int | None = Field(None, ge=10, le=120, strict=True)
    shuttle_capacity_people: int | None = Field(None, ge=0, le=120, strict=True)
    shuttle_headway_minutes: int | None = Field(None, ge=5, le=60, strict=True)
    taxi_interval_seconds: int | None = Field(None, ge=10, le=240, strict=True)
    shore_minutes: int | None = Field(None, ge=60, le=720, strict=True)
    boarding_minutes: int | None = Field(None, ge=20, le=180, strict=True)
    group_cap: int | None = Field(None, ge=50, le=2000, strict=True)


@app.post('/api/cruise-terminal/scenario', dependencies=[Depends(require_control)], tags=['Control'])
def cruise_terminal_scenario(body: CruiseScenario):
    """Update terminal scenario settings; applies to ship calls created afterwards."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(422, 'Provide at least one terminal setting.')
    with db.state_transaction() as (session, row, state):
        result = cruise_terminal.apply_scenario(state, **updates)
        result['run_id'] = row.run_id
        return result


@app.get('/api/sensors', tags=['Operations'])
def sensors():
    snapshot = state_snapshot()
    return {'items': snapshot['sensors'], 'clock': snapshot['clock']}


def infrastructure_view():
    with db.Session() as session:
        row = session.get(db.Simulation, 1)
        result = infrastructure.infrastructure_snapshot(row.payload)
        return {**result, 'run_id': row.run_id, 'synthetic': True,
            'world_version': WORLD['version'],
            'simulated_at': datetime.fromtimestamp(row.payload['t'], timezone.utc).isoformat()}


@app.get('/api/infrastructure', tags=['Infrastructure'])
def transit_infrastructure():
    """Activity-derived facility utility loads, cumulative meters and disruptions."""
    return infrastructure_view()


@app.get('/api/infrastructure/facilities/{facility_id}', tags=['Infrastructure'])
def infrastructure_facility(facility_id: str):
    result = infrastructure_view()
    facility = next((f for f in result['facilities'] if f['id'] == facility_id), None)
    if facility is None:
        raise HTTPException(404, 'Infrastructure facility not found.')
    return {'facility': facility, 'run_id': result['run_id'], 'synthetic': True,
        'installed_at': result['installed_at'], 'observed_at': result['observed_at'],
        'simulated_at': result['simulated_at']}


@app.get('/api/road-traffic', tags=['Operations'])
def road_traffic(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    """Authored commuter road cohorts; representation weights are distinct from person dots."""
    snapshot = state_snapshot()
    result = snapshot['road_traffic']
    vehicles = result.get('vehicles', [])
    return {**result, 'vehicles': vehicles[offset:offset+limit], 'total': len(vehicles),
        'offset': offset, 'limit': limit, 'clock': snapshot['clock'],
        'run_id': snapshot['run_id'], 'synthetic': True}


class InfrastructureDisruption(Strict):
    facility_id: str = Field(min_length=1, max_length=100)
    utility: Literal['power', 'elevators', 'internet', 'water', 'sewer']
    duration_minutes: int = Field(30, ge=1, le=1440, strict=True)
    severity: float = Field(1.0, ge=0.01, le=1.0, allow_inf_nan=False)

    @field_validator('severity', mode='before')
    @classmethod
    def numeric_severity(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError('severity must be a number from 0.01 through 1.')
        return value


@app.post('/api/infrastructure/disruptions', dependencies=[Depends(require_control)],
    status_code=201, tags=['Infrastructure'])
def disrupt_infrastructure(body: InfrastructureDisruption):
    with db.state_transaction() as (_session, row, state):
        try:
            disruption = infrastructure.create_disruption(state, **body.model_dump())
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {'disruption': disruption, 'run_id': row.run_id, 'synthetic': True}


@app.post('/api/infrastructure/disruptions/{disruption_id}/restore',
    dependencies=[Depends(require_control)], tags=['Infrastructure'])
def restore_infrastructure(disruption_id: str):
    with db.state_transaction() as (_session, row, state):
        try:
            disruption = infrastructure.restore_disruption(state, disruption_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        return {'disruption': disruption, 'run_id': row.run_id, 'synthetic': True}


@app.get('/api/incidents', tags=['Incidents'])
def incidents():
    snapshot = state_snapshot()
    return {'items': snapshot['incidents'], 'clock': snapshot['clock'], 'run_id': snapshot['run_id']}


class Control(Strict):
    paused: bool | None = Field(None, strict=True)
    speed: Literal[1, 15, 30, 60, 180, 300, 600] | None = None

    @field_validator('speed', mode='before')
    @classmethod
    def integer_speed(cls, value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError('speed must be an integer.')
        return value


@app.post('/api/control', dependencies=[Depends(require_control)], tags=['Control'])
def control(body: Control):
    with db.state_transaction() as (session, row, state):
        for key, value in body.model_dump(exclude_none=True).items():
            state[key] = value
        return {'clock': engine.snapshot(state)['clock'], 'run_id': row.run_id}


class Scenario(Strict):
    kind: Literal['collision', 'medical', 'crowd_surge', 'signal_failure', 'storm']
    place_id: str = Field(min_length=1, max_length=100)
    casualties: int = Field(0, ge=0, le=30, strict=True)
    route_id: str | None = Field(None, min_length=1, max_length=100)


@app.post('/api/scenarios', dependencies=[Depends(require_control)], status_code=201, tags=['Incidents'])
def scenario(body: Scenario):
    if body.place_id not in {p['id'] for p in CITY['places']}:
        raise HTTPException(422, 'Unknown place_id.')
    if body.route_id and body.route_id not in {r['id'] for r in CITY['routes']}:
        raise HTTPException(422, 'Unknown route_id.')
    with db.state_transaction() as (session, row, state):
        incident = engine.create_incident(state, body.kind, body.place_id, body.casualties, body.route_id)
        services.enqueue_incidents(session, row, state)
        return incident


class Jump(Strict):
    date: date
    seed: int = Field(42, ge=0, le=2147483647)

    @field_validator('date')
    @classmethod
    def supported_year(cls, value):
        if not 2020 <= value.year <= 2100:
            raise ValueError('Choose a year from 2020 through 2100.')
        return value


@app.post('/api/scenarios/jump', dependencies=[Depends(require_control)], tags=['Control'])
def jump(body: Jump):
    from zoneinfo import ZoneInfo
    start = datetime.combine(body.date, datetime.min.time().replace(hour=8), ZoneInfo('America/Moncton'))
    fresh = engine.create_state(body.seed, start.isoformat())
    fresh['run_id'] = str(uuid.uuid4())
    with db.state_transaction() as (session, row, old):
        session.add(db.ArchivedRun(id=row.run_id, payload=old))
        fresh['speed'] = old['speed']
        fresh['paused'] = old['paused']
        fresh['workplace_revision'] = old.get('workplace_revision', 0)
        fresh['commute_workplaces'] = old.get('commute_workplaces', [])
        row.run_id = fresh['run_id']
        row.payload = fresh
    return {'run_id': fresh['run_id'], 'clock': engine.snapshot(fresh)['clock'], 'previous_run_archived': True}


@app.get('/api/runs', tags=['System'])
def runs(limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
    with db.Session() as session:
        current = session.get(db.Simulation, 1)
        items = [{'id': r.id, 'archived_at': r.archived_at.isoformat()} for r in session.scalars(
            select(db.ArchivedRun).order_by(db.ArchivedRun.archived_at.desc()).offset(offset).limit(limit))]
        return {'current_run_id': current.run_id, 'archived': items, 'offset': offset, 'limit': limit}


@app.get('/api/runs/{run_id}', tags=['System'])
def run_detail(run_id: str):
    with db.Session() as session:
        current = session.get(db.Simulation, 1)
        if current.run_id == run_id:
            return engine.snapshot(current.payload)
        archived = session.get(db.ArchivedRun, run_id)
        if not archived:
            raise HTTPException(404, 'Run not found.')
        return engine.snapshot(archived.payload)


class IntegrationReport(Strict):
    schema_version: Literal['1.0'] = '1.0'
    synthetic: Literal[True] = True
    source: str = Field(min_length=1, max_length=80, pattern=r'^[a-zA-Z0-9_-]+$')
    external_id: str = Field(min_length=1, max_length=160)
    incident_id: str | None = Field(None, min_length=1, max_length=120)
    kind: Literal['incident_status', 'capacity', 'telemetry'] = 'incident_status'
    status: Literal['dispatching', 'on_scene', 'transporting', 'resolved', 'observed'] = 'observed'
    source_sim_time: datetime | None = None
    details: dict = Field(default_factory=dict)

    @field_validator('synthetic', mode='before')
    @classmethod
    def literal_synthetic(cls, value):
        if value is not True:
            raise ValueError('synthetic must be the JSON boolean true.')
        return value

    @model_validator(mode='after')
    def validate_report(self):
        if self.kind == 'incident_status' and not self.incident_id:
            raise ValueError('incident_status requires incident_id.')
        if self.source_sim_time is not None and self.source_sim_time.tzinfo is None:
            raise ValueError('source_sim_time must include a timezone offset.')
        if len(json.dumps(self.details, allow_nan=False)) > 65536:
            raise ValueError('Report details must be at most 64 KiB.')
        return self


@app.post('/api/integrations/reports', dependencies=[Depends(require_report)], tags=['Integrations'])
def report(body: IntegrationReport):
    try:
        result = services.record_report(body.model_dump(mode='json'))
        return JSONResponse(result, status_code=200 if result['duplicate'] else 201)
    except services.Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None


@app.get('/api/integrations/reports', tags=['Integrations'])
def reports(source: str | None = None, incident_id: str | None = None,
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    with db.Session() as session:
        query = select(db.Report).order_by(db.Report.observed_at.desc())
        if source:
            query = query.where(db.Report.source == source)
        if incident_id:
            query = query.where(db.Report.incident_id == incident_id)
        return {'items': [{'id': r.id, **r.payload, 'observed_at': r.observed_at.isoformat(),
            'transit_sim_time': r.transit_sim_time, 'run_id': r.run_id}
            for r in session.scalars(query.offset(offset).limit(limit))], 'offset': offset, 'limit': limit}


@app.get('/api/integrations/status', tags=['Integrations'])
def integration_status():
    from .polling import policy
    with db.Session() as session:
        jobs = session.scalars(select(db.Outbox).order_by(db.Outbox.created_at.desc()).limit(50)).all()
        polls = {p.outbox_id: p for p in session.scalars(select(db.FeedbackPoll)
            .where(db.FeedbackPoll.outbox_id.in_([j.id for j in jobs])))}
        items = [{'id': j.id, 'incident_id': j.id, 'run_id': j.run_id, 'status': j.status, 'attempts': j.attempts,
            'remote_incident_id': j.remote_incident_id, 'error': j.error, 'created_at': j.created_at.isoformat(),
            'sent_at': j.sent_at.isoformat() if j.sent_at else None, 'body_hash': j.body_hash} for j in jobs]
        for item in items:
            poll = polls.get(item['id'])
            item['feedback_poll'] = None if poll is None else {
                'state': poll.state,
                'next_poll_at': poll.next_poll_at.isoformat() if poll.state != 'retired' else None,
                'interval_seconds': (poll.details or {}).get('interval_seconds'),
                'failures': poll.failures, 'last_error': poll.last_error,
                'terminal_since': poll.terminal_since.isoformat() if poll.terminal_since else None,
                'updated_at': poll.updated_at.isoformat()}
        connections = [{'id': c.id, 'name': {'911':'911 Command','hospital':'Hospital & EMS'}.get(c.id,c.id),
            'status': c.status, 'observed_at': c.observed_at.isoformat(), **c.details}
            for c in session.scalars(select(db.Connection))]
        counts = dict(session.execute(select(db.Outbox.status, func.count()).group_by(db.Outbox.status)).all())
        poll_counts = dict(session.execute(select(db.FeedbackPoll.state, func.count())
            .group_by(db.FeedbackPoll.state)).all())
        awaiting_schedule = session.scalar(select(func.count()).select_from(db.Outbox)
            .outerjoin(db.FeedbackPoll, db.FeedbackPoll.outbox_id == db.Outbox.id)
            .where(db.Outbox.status == 'sent', db.Outbox.remote_incident_id.is_not(None),
                db.FeedbackPoll.outbox_id.is_(None)))
    return {'outbox': items, 'counts': counts, 'connections': connections, 'reports': reports(limit=30, offset=0)['items'],
        'polling': {'counts': poll_counts, 'awaiting_schedule': awaiting_schedule, 'policy': policy()},
        'report_endpoint': '/api/integrations/reports', 'mcp_endpoint': 'http://127.0.0.1:8826/mcp',
        'notes': 'Feedback is observed from separate simulator clocks; paused source services remain paused.'}


@app.post('/api/integrations/outbox/{incident_id}/refresh', dependencies=[Depends(require_control)], tags=['Integrations'])
def refresh_feedback(incident_id: str):
    """Queue a read-only reconciliation without creating another dispatch."""
    with db.Session.begin() as session:
        job = session.get(db.Outbox, incident_id)
        if not job:
            raise HTTPException(404, 'Dispatch not found.')
        if job.status != 'sent' or not job.remote_incident_id:
            raise HTTPException(409, 'Feedback refresh requires a delivered incident.')
        services.wake_feedback(session, incident_id)
    return {'status': 'queued', 'incident_id': incident_id}


@app.post('/api/integrations/outbox/{incident_id}/retry', dependencies=[Depends(require_control)], tags=['Integrations'])
def retry_dispatch(incident_id: str):
    with db.Session.begin() as session:
        job = session.get(db.Outbox, incident_id, with_for_update=True)
        if not job:
            raise HTTPException(404, 'Dispatch not found.')
        if job.status == 'sent':
            raise HTTPException(409, 'Already delivered; no retry needed.')
        if job.lease_until and job.lease_until.replace(tzinfo=timezone.utc) > db.now():
            raise HTTPException(409, 'A delivery attempt is in progress.')
        job.status = 'pending'
        job.next_attempt_at = db.now()
        job.error = None
    return {'status': 'pending', 'incident_id': incident_id}


# Northstar coordinated time; independent mode remains available for isolated tests.
from app.northstar_clock import attach as attach_shared_clock
attach_shared_clock(app, 'transit')


# Source-owned, read-only live people locations.
from app.people_locations import attach_people_locations
attach_people_locations(app, 'transit')
