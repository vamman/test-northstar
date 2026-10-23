"""Scoped registration API and cached, persisted workplace definitions."""
from copy import deepcopy
from datetime import datetime
import json
import re
import uuid

from fastapi import Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select

from . import commutes, db, services
from .catalog import CITY, NODES, ROADS, WORLD, district_at


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Location(Strict):
    crs: str = 'northstar-local-m-v1'
    units: str = 'm'
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)
    district_id: str

    @model_validator(mode='after')
    def canonical(self):
        if self.crs != 'northstar-local-m-v1' or self.units != 'm':
            raise ValueError('Coordinates must use northstar-local-m-v1 in metres.')
        if district_at(self.x, self.y) != self.district_id:
            raise ValueError('Location must fall inside its canonical Northstar district.')
        return self


class Business(Strict):
    id: str = Field(min_length=1, max_length=120, pattern=r'^[A-Za-z0-9_:-]+$')
    name: str = Field(min_length=1, max_length=120)
    sector: str = Field(min_length=1, max_length=100)
    worker_count: int = Field(ge=0, le=50000, strict=True)
    floors: list[int] = Field(default_factory=list, max_length=120)

    @field_validator('floors')
    @classmethod
    def floor_numbers(cls, values):
        if any(isinstance(v, bool) or not 1 <= v <= 120 for v in values):
            raise ValueError('Business floors must be numbers between 1 and 120.')
        return values


class WorkplaceDefinition(Strict):
    id: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    name: str = Field(min_length=1, max_length=120)
    location: Location
    floor_count: int | None = Field(None, ge=1, le=120, strict=True)
    worker_count: int | None = Field(None, ge=0, le=50000, strict=True)
    business_count: int | None = Field(None, ge=0, le=100, strict=True)
    tenancy_type: str | None = Field(None, max_length=60)
    businesses: list[Business] = Field(default_factory=list, max_length=100)


class WorkerDefinition(Strict):
    worker_id: str = Field(min_length=1, max_length=140, pattern=r'^[A-Za-z0-9_:-]+$')
    business_id: str | None = Field(None, min_length=1, max_length=120)
    home_place_id: str = Field(min_length=1, max_length=100)
    role_group: str = Field(default='office_staff', min_length=1, max_length=60)
    work_mode: str = 'office'
    commute_mode: str = 'transit'
    shift_start: str = '08:00'
    shift_end: str = '17:00'
    shift_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4], max_length=7)
    roster_rotation: str = 'weekdays'
    roster_offset: int = Field(0, ge=0, le=7, strict=True)
    hybrid_days: list[int] = Field(default_factory=lambda: [0, 1, 2], max_length=7)

    @field_validator('shift_start', 'shift_end')
    @classmethod
    def hhmm(cls, value):
        if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
            raise ValueError('Use a 24-hour HH:MM local shift time.')
        return value

    @field_validator('shift_days', 'hybrid_days', mode='before')
    @classmethod
    def weekdays(cls, value):
        if not isinstance(value, list) or any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 6 for v in value) or len(value) != len(set(value)):
            raise ValueError('Working days must be distinct integers 0 (Monday) through 6 (Sunday).')
        return value

    @model_validator(mode='after')
    def supported(self):
        if self.home_place_id not in commutes.HOME_PLACES:
            raise ValueError('Unknown home_place_id; read /api/commutes/catalog for canonical residential anchors.')
        if self.work_mode not in {'office', 'hybrid', 'remote'}:
            raise ValueError('work_mode must be office, hybrid or remote.')
        if self.commute_mode not in {'walk', 'cycle', 'car', 'transit'}:
            raise ValueError('commute_mode must be walk, cycle, car or transit.')
        if self.roster_rotation not in {'weekdays', 'six_on_two_off', 'five_on_two_off'}:
            raise ValueError('Unsupported roster rotation.')
        if self.roster_rotation == 'five_on_two_off' and self.roster_offset > 6:
            raise ValueError('A seven-day rotation permits offsets 0 through 6.')
        if self.shift_start == self.shift_end:
            raise ValueError('A shift must have nonzero duration and be shorter than 24 hours.')
        return self


class Registration(Strict):
    schema_version: str = '1.0'
    synthetic: bool = True
    source: str = 'northstar_office'
    external_id: str = Field(min_length=1, max_length=160)
    source_sim_time: datetime
    workplace: WorkplaceDefinition
    workers: list[WorkerDefinition] = Field(default_factory=list, max_length=500)

    @field_validator('synthetic', mode='before')
    @classmethod
    def boolean_true(cls, value):
        if value is not True:
            raise ValueError('synthetic must be the JSON boolean true.')
        return value

    @model_validator(mode='after')
    def bounded(self):
        if self.schema_version != '1.0' or self.source != 'northstar_office':
            raise ValueError('Only schema 1.0 synthetic northstar_office registrations are accepted.')
        if self.source_sim_time.tzinfo is None:
            raise ValueError('source_sim_time must include a timezone offset.')
        if len({w.worker_id for w in self.workers}) != len(self.workers):
            raise ValueError('Worker IDs must be unique within a registration chunk.')
        if len({b.id for b in self.workplace.businesses}) != len(self.workplace.businesses):
            raise ValueError('Business IDs must be unique.')
        businesses = {b.id for b in self.workplace.businesses}
        if businesses and any(w.business_id and w.business_id not in businesses for w in self.workers):
            raise ValueError('A worker business_id must belong to this workplace.')
        if len(self.model_dump_json()) > 1024 * 1024:
            raise ValueError('A registration envelope must be at most 1 MiB.')
        return self


_registry_cache = {}


def registry(session, state):
    revision = state.get('workplace_revision', 0)
    key = (str(db.engine.url), state['run_id'], revision)
    if key not in _registry_cache:
        places = {row.id: deepcopy(row.payload) for row in session.scalars(select(db.Workplace))}
        workers = {row.id: deepcopy(row.payload) for row in session.scalars(select(db.OfficeWorker))}
        _registry_cache.clear()
        _registry_cache[key] = {'revision': revision, 'workplaces': places, 'workers': workers}
    return _registry_cache[key]


def register(payload):
    digest = services.body_hash(payload)
    with db.state_transaction() as (session, row, state):
        previous = session.scalar(select(db.Report).where(db.Report.source == payload['source'], db.Report.external_id == payload['external_id']))
        if previous:
            if previous.body_hash != digest:
                raise services.Conflict('This source/external_id already identifies a different immutable registration.')
            return {**previous.payload['receipt'], 'duplicate': True}
        ident = payload['workplace']['id']
        ids = [w['worker_id'] for w in payload['workers']]
        existing = {w.id: w for w in session.scalars(select(db.OfficeWorker).where(db.OfficeWorker.id.in_(ids)))} if ids else {}
        total = session.scalar(select(func.count()).select_from(db.OfficeWorker))
        cap = commutes.max_registrations()
        if total + len(ids) - len(existing) > cap:
            raise ValueError(f'The persisted office roster is limited to {cap:,} workers; active people remain capped at 5,000.')
        workplace = session.get(db.Workplace, ident)
        if workplace is not None and workplace.payload.get('location') != payload['workplace']['location']:
            raise services.Conflict('A registered workplace location is immutable; register a distinct building ID for a new destination.')
        if workplace is None:
            workplace = db.Workplace(id=ident, payload={}); session.add(workplace)
        workplace.payload = {**payload['workplace'], 'source': payload['source'],
            'source_sim_time': payload['source_sim_time'], 'observed_at': db.now().isoformat(),
            'clock_policy': commutes.CLOCK_POLICY}
        workplace.updated_at = db.now()
        for definition in payload['workers']:
            worker = existing.get(definition['worker_id'])
            if worker is None:
                worker = db.OfficeWorker(id=definition['worker_id'], workplace_id=ident, payload={}); session.add(worker)
            elif worker.workplace_id != ident:
                raise services.Conflict('A stable worker ID cannot be silently reassigned to another workplace.')
            worker.payload = {**definition, 'workplace_id': ident, 'source_sim_time': payload['source_sim_time']}
            worker.updated_at = db.now()
        state['workplace_revision'] = state.get('workplace_revision', 0) + 1
        session.flush()
        # Invalidate process-local cache; the clock worker sees the revision itself.
        _registry_cache.clear()
        receipt = {'workplace_id': ident, 'place_id': 'office:' + ident, 'accepted_workers': len(ids),
            'registered_workers': session.scalar(select(func.count()).select_from(db.OfficeWorker).where(db.OfficeWorker.workplace_id == ident)),
            'revision': state['workplace_revision'], 'clock_policy': commutes.CLOCK_POLICY,
            'transit_sim_time': commutes.iso(state['t']), 'source_sim_time': payload['source_sim_time'],
            'observed_at': db.now().isoformat(), 'synthetic': True, 'duplicate': False}
        session.add(db.Report(id=str(uuid.uuid4()), source=payload['source'], external_id=payload['external_id'],
            body_hash=digest, payload={'kind': 'workplace_registration', 'schema_version': payload['schema_version'], 'synthetic': True,
                'source': payload['source'], 'external_id': payload['external_id'], 'workplace_id': ident,
                'source_sim_time': payload['source_sim_time'], 'worker_count': len(ids), 'receipt': receipt},
            transit_sim_time=commutes.iso(state['t']), run_id=row.run_id))
        return receipt


def city_overlay():
    with db.Session() as session:
        workplaces = [deepcopy(row.payload) for row in session.scalars(select(db.Workplace))]
    if not workplaces:
        return CITY
    places = [commutes.workplace_place(w) for w in workplaces]
    roads = []
    for place in places:
        nearest = min(NODES, key=lambda n: (NODES[n][0] - place['x']) ** 2 + (NODES[n][1] - place['y']) ** 2)
        roads.append({'id': place['id'] + ':access', 'name': place['name'] + ' access', 'class': 'access',
            'geometry': {'type': 'LineString', 'coordinates': [[place['x'], place['y']], list(NODES[nearest])]}, 'synthetic': True})
    bounds = dict(CITY['bounds'])
    for place in places:
        bounds['min_x'] = min(bounds['min_x'], place['x'] - 1500); bounds['max_x'] = max(bounds['max_x'], place['x'] + 1500)
        bounds['min_y'] = min(bounds['min_y'], place['y'] - 1500); bounds['max_y'] = max(bounds['max_y'], place['y'] + 1500)
    return {**CITY, 'bounds': bounds, 'places': CITY['places'] + places,
        'roads': ROADS + roads, 'office_workplaces': workplaces,
        'buildings': CITY['buildings'] + [{'id': 'office:' + w['id'], 'x': w['location']['x'] - 45,
            'y': w['location']['y'] - 35, 'width': 90, 'height': 70, 'height_floors': w.get('floor_count') or 1,
            'kind': 'office', 'district_id': w['location']['district_id'], 'name': w['name']} for w in workplaces]}


def attach_routes(app, require_report):
    @app.post('/api/integrations/workplaces', dependencies=[Depends(require_report)], tags=['Office commutes'])
    def register_workplace(body: Registration):
        try:
            result = register(body.model_dump(mode='json'))
            return JSONResponse(result, status_code=200 if result['duplicate'] else 201)
        except services.Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get('/api/commutes/catalog', tags=['Office commutes'])
    def catalog():
        return {'home_places': list(commutes.HOME_PLACES.values()), 'synthetic': True,
            'modes': ['walk', 'cycle', 'car', 'transit'], 'crs': 'northstar-local-m-v1', 'units': 'm',
            'clock_policy': commutes.CLOCK_POLICY, 'max_workers_per_registration': 500,
            'public_transit_policy': 'Existing single route only; both ends within 1.2 km straight-line / 1.8 km actual walking access. Otherwise explicit car fallback.'}

    @app.get('/api/workplaces', tags=['Office commutes'])
    def list_workplaces():
        with db.Session() as session:
            row = session.get(db.Simulation, 1); state = row.payload
            counts = dict(session.execute(select(db.OfficeWorker.workplace_id, func.count()).group_by(db.OfficeWorker.workplace_id)).all())
            places = [{**p.payload, 'place_id': 'office:' + p.id, 'registered_workers': counts.get(p.id, 0)} for p in session.scalars(select(db.Workplace))]
            for place in places:
                place['active_commuters'] = sum(p.get('workplace_id') == place['id'] for p in state['people'])
                place['observed_arrivals_at_work'] = sum(p['workplace_id'] == place['id'] for p in state.get('commute_at_work', {}).values())
            return {'items': places, 'total': len(places), 'revision': state.get('workplace_revision', 0),
                'transit_sim_time': commutes.iso(state['t']), 'clock_policy': commutes.CLOCK_POLICY,
                'office_occupancy_policy': 'independent_source_schedule', 'synthetic': True}

    @app.get('/api/commutes', tags=['Office commutes'])
    def list_commutes(workplace_id: str | None = None, worker_id: str | None = None, direction: str | None = None,
        status: str | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
        with db.Session() as session:
            state = session.get(db.Simulation, 1).payload
            definitions = registry(session, state)
            items = [{**{k: p.get(k) for k in ('id', 'trip_id', 'x', 'y', 'mode', 'status', 'workplace_id', 'business_id', 'departed_at', 'fallback_reason', 'source_sim_time')},
                'worker_id': p['office_worker_id'], 'direction': p['commute_direction'], 'leg': p['commute_leg']}
                for p in state['people'] if p.get('office_worker_id')]
            for event in state.get('commute_pending', []):
                worker = definitions['workers'].get(event[1], {})
                items.append({'worker_id': event[1], 'workplace_id': worker.get('workplace_id'), 'business_id': worker.get('business_id'),
                    'direction': event[4], 'status': 'deferred', 'requested_at': commutes.iso(event[0]), 'reason': 'waiting_for_active_sample_capacity'})
            items += list(reversed(state.get('commute_history', [])))
            items = [i for i in items if (not workplace_id or i.get('workplace_id') == workplace_id) and
                (not worker_id or i.get('worker_id') == worker_id) and (not direction or i.get('direction') == direction) and (not status or i.get('status') == status)]
            result = commutes.summary(state)
            if workplace_id:
                result['contact_exposure'] = [e for e in result['contact_exposure'] if e['workplace_id'] == workplace_id]
            return {'items': items[offset:offset + limit], 'total': len(items), 'limit': limit, 'offset': offset,
                'summary': result, 'completed_legs': [leg for leg in reversed(state.get('commute_leg_history', []))
                    if (not workplace_id or leg['workplace_id'] == workplace_id) and (not worker_id or leg['worker_id'] == worker_id)
                    and (not direction or leg['direction'] == direction)][:limit],
                'transit_sim_time': commutes.iso(state['t']), 'run_id': state['run_id'],
                'history_limit': commutes.HISTORY_LIMIT, 'history_scope': 'Recent completed/missed journeys; totals retain all trips in this run.'}
