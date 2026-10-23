"""Persistent office itineraries on the existing graph, under Transit's local clock.

Registries live in database tables. The clock payload stores only a deterministic
next-departure heap, actual journeys, bounded history and exposure aggregates.
No roster-wide scan occurs on ordinary ten-second steps.
"""
from collections import Counter
from datetime import datetime, timedelta
from functools import lru_cache
import hashlib
import heapq
import math
from zoneinfo import ZoneInfo

from .catalog import GRAPH, NODES, PLACE_INDEX, ROUTES, ROUTE_INDEX, WORLD
from . import population_client

TZ = ZoneInfo('America/Moncton')
MAX_REGISTRATIONS = 50000


def max_registrations():
    """Roster cap scales with the shared regional population (baseline 50,000 at 200k)."""
    return int(MAX_REGISTRATIONS * population_client.population_ratio())
RELEASE_PER_STEP = 128
HISTORY_LIMIT = 2000
CLOCK_POLICY = 'interpret_recurring_rosters_in_transit_local_time'
HOME_PLACES = {n['id']: {'id': n['id'], 'name': n['name'], 'x': n['x'] * 1000,
    'y': n['y'] * 1000, 'district_id': n['parent_district_id'], 'kind': 'residential'}
    for n in WORLD['neighbourhoods']}


def stable_int(value):
    return int(hashlib.sha256(value.encode()).hexdigest()[:12], 16)


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, TZ).isoformat(timespec='seconds')


@lru_cache(maxsize=4096)
def coordinate_path(origin, destination):
    """Add endpoint access connectors without mutating the authoritative graph."""
    start = min(NODES, key=lambda n: math.dist(origin, NODES[n]))
    end = min(NODES, key=lambda n: math.dist(destination, NODES[n]))
    distances = {start: 0.0}; previous = {}; queue = [(0.0, start)]
    while queue:
        distance, node = heapq.heappop(queue)
        if node == end:
            break
        if distance != distances.get(node):
            continue
        for target, length in GRAPH[node]:
            candidate = distance + length
            if candidate < distances.get(target, math.inf):
                distances[target] = candidate; previous[target] = node
                heapq.heappush(queue, (candidate, target))
    cursor = end; path = [cursor]
    while cursor != start:
        cursor = previous[cursor]; path.append(cursor)
    points = [list(origin)]
    for node in reversed(path):
        point = list(NODES[node])
        if point != points[-1]:
            points.append(point)
    if list(destination) != points[-1]:
        points.append(list(destination))
    if len(points) == 1:
        points.append(points[0][:])
    distances = [0.0]
    for a, b in zip(points, points[1:]):
        distances.append(distances[-1] + math.dist(a, b))
    return points, distances


def point(place):
    return (float(place['x']), float(place['y']))


def workplace_place(workplace):
    return {'id': 'office:' + workplace['id'], 'name': workplace['name'], 'kind': 'office',
            **{k: workplace['location'][k] for k in ('x', 'y', 'district_id')}}


def direct_leg(origin, destination, mode):
    return {'origin_id': origin['id'], 'destination_id': destination['id'],
            'origin_xy': list(point(origin)), 'destination_xy': list(point(destination)),
            'mode': mode, 'route_id': None, 'direction': None}


@lru_cache(maxsize=4096)
def route_plan(home_id, office_id, office_x, office_y, mode, direction):
    home = HOME_PLACES[home_id]
    office = {'id': 'office:' + office_id, 'x': office_x, 'y': office_y}
    origin, destination = (home, office) if direction == 'outbound' else (office, home)
    points, lengths = coordinate_path(point(origin), point(destination))
    fallback = None
    if mode == 'transit':
        candidates = []
        for route in ROUTES:
            a = min(range(len(route['stops'])), key=lambda i: math.dist(point(origin), point(PLACE_INDEX[route['stops'][i]])))
            b = min(range(len(route['stops'])), key=lambda i: math.dist(point(destination), point(PLACE_INDEX[route['stops'][i]])))
            if a == b:
                continue
            first, last = PLACE_INDEX[route['stops'][a]], PLACE_INDEX[route['stops'][b]]
            if math.dist(point(origin), point(first)) > 1200 or math.dist(point(destination), point(last)) > 1200:
                continue
            access = coordinate_path(point(origin), point(first))[1][-1]
            egress = coordinate_path(point(last), point(destination))[1][-1]
            if access > 1800 or egress > 1800:
                continue
            duration = (access + egress) / 1.35 + abs(route['stop_distances'][b] - route['stop_distances'][a]) / route['speed_mps'] + route['headway_minutes'] * 30 + abs(b - a) * 25
            legs = [direct_leg(origin, first, 'walk'), {
                **direct_leg(first, last, route['mode']), 'route_id': route['id'], 'direction': 1 if b > a else -1}, direct_leg(last, destination, 'walk')]
            candidates.append((duration, route['id'], legs))
        if candidates:
            duration, _, legs = min(candidates)
            return legs, duration, None
        mode = 'car'; fallback = 'no_scheduled_public_transit_coverage'
    elif mode == 'walk' and lengths[-1] > 8000:
        mode = 'car'; fallback = 'walking_distance_exceeds_authored_limit'
    elif mode == 'cycle' and lengths[-1] > 25000:
        mode = 'car'; fallback = 'cycling_distance_exceeds_authored_limit'
    speed = {'walk': 1.35, 'cycle': 4.8, 'car': 8.0}[mode]
    return [direct_leg(origin, destination, mode)], lengths[-1] / speed, fallback


def itinerary(worker, workplace, direction, departure=None):
    legs, duration, fallback = route_plan(worker['home_place_id'], workplace['id'],
        workplace['location']['x'], workplace['location']['y'], worker['commute_mode'], direction)
    if departure is not None:
        from .engine import _in_service
        transit = next((leg for leg in legs if leg['route_id']), None)
        if transit and not _in_service(ROUTE_INDEX[transit['route_id']], departure):
            legs, duration, _ = route_plan(worker['home_place_id'], workplace['id'],
                workplace['location']['x'], workplace['location']['y'], 'car', direction)
            fallback = 'scheduled_transit_not_in_service_at_departure'
    return legs, duration, fallback


def works_on(worker, day):
    if worker['work_mode'] == 'remote' or day.weekday() not in worker['shift_days']:
        return False
    if worker['work_mode'] == 'hybrid' and day.weekday() not in worker['hybrid_days']:
        return False
    rotation = worker['roster_rotation']
    if rotation == 'weekdays':
        return day.weekday() < 5
    period, on = (8, 6) if rotation == 'six_on_two_off' else (7, 5)
    return (day.toordinal() + worker['roster_offset']) % period < on


def next_departure(worker, workplace, after):
    if worker['work_mode'] == 'remote' or not worker['shift_days'] or (worker['work_mode'] == 'hybrid' and not worker['hybrid_days']):
        return None
    local = datetime.fromtimestamp(after, TZ)
    for offset in range(18):
        day = (local + timedelta(days=offset)).date()
        if not works_on(worker, day):
            continue
        start = datetime.combine(day, datetime.strptime(worker['shift_start'], '%H:%M').time(), TZ)
        end = datetime.combine(day, datetime.strptime(worker['shift_end'], '%H:%M').time(), TZ)
        if end <= start:
            end += timedelta(days=1)
        _, duration, _ = itinerary(worker, workplace, 'outbound')
        spread = stable_int(worker['worker_id'] + day.isoformat()) % 901
        departure = start.timestamp() - duration - 300 - spread
        if departure >= after:
            return [departure, worker['worker_id'], start.timestamp(), end.timestamp(), 'outbound']
    return None


def initialize(state):
    state.setdefault('workplace_revision', 0)
    state.setdefault('commute_calendar_revision', -1)
    state.setdefault('commute_events', [])
    state.setdefault('commute_pending', [])
    state.setdefault('commute_active_workers', {})
    state.setdefault('commute_at_work', {})
    state.setdefault('commute_history', [])
    state.setdefault('commute_leg_history', [])
    state.setdefault('commute_workplaces', [])
    state.setdefault('commute_contacts', {})
    state.setdefault('commute_totals', {'requested': 0, 'materialized': 0, 'completed': 0, 'missed': 0,
                                      'outbound_completed': 0, 'return_completed': 0})


def prepare(state, registry):
    initialize(state)
    if registry is None or state['commute_calendar_revision'] == registry['revision']:
        return
    # Rebuild only when definitions change. Existing real trips and requests survive.
    events = [e for e in state['commute_events'] if e[4] == 'return']
    for ident, worker in registry['workers'].items():
        workplace = registry['workplaces'][worker['workplace_id']]
        event = next_departure(worker, workplace, state['t'])
        if event:
            events.append(event)
    heapq.heapify(events)
    state['commute_events'] = events
    state['commute_calendar_revision'] = registry['revision']
    counts = Counter(worker['workplace_id'] for worker in registry['workers'].values())
    state['commute_workplaces'] = [{**place, 'registered_workers': counts[place['id']],
        'place_id': 'office:' + place['id']} for place in registry['workplaces'].values()]


def _record(state, worker, event, status, **extra):
    _, _, begin, end, direction = event
    row = {'worker_id': worker['worker_id'], 'workplace_id': worker['workplace_id'],
           'business_id': worker.get('business_id'), 'direction': direction, 'status': status,
           'shift_date': datetime.fromtimestamp(begin, TZ).date().isoformat(),
           'scheduled_shift_start': iso(begin), 'scheduled_shift_end': iso(end),
           'recorded_at': iso(state['t']), 'source_sim_time': worker.get('source_sim_time'),
           'clock_policy': CLOCK_POLICY, **extra}
    state['commute_history'].append(row)
    if len(state['commute_history']) > HISTORY_LIMIT:
        del state['commute_history'][:-HISTORY_LIMIT]
    return row


def _begin_leg(person, leg):
    person.update({key: leg[key] for key in ('origin_id', 'destination_id', 'mode', 'route_id', 'direction')})
    person.update(status='queued' if leg['route_id'] else 'moving', vehicle_id=None,
                  distance=0.0, x=leg['origin_xy'][0], y=leg['origin_xy'][1])


def release(state, dt, registry):
    if registry is None:
        return
    events = state['commute_events']; pending = state['commute_pending']
    while events and events[0][0] <= state['t']:
        event = heapq.heappop(events)
        worker = registry['workers'].get(event[1])
        if not worker:
            continue
        if event[4] == 'outbound':
            following = next_departure(worker, registry['workplaces'][worker['workplace_id']], event[0] + 1)
            if following:
                heapq.heappush(events, following)
        state['commute_totals']['requested'] += 1
        heapq.heappush(pending, event)
    # Respect both the shared person ceiling and a bounded release burst.
    released = 0
    while pending:
        event = pending[0]; worker = registry['workers'].get(event[1])
        expired = event[4] == 'outbound' and event[3] <= state['t']
        busy = event[1] in state['commute_active_workers'] or (event[4] == 'outbound' and event[1] in state['commute_at_work'])
        if not worker or expired or busy:
            heapq.heappop(pending); state['commute_totals']['missed'] += 1
            if worker:
                _record(state, worker, event, 'missed', reason='shift_ended_before_capacity_available' if expired else 'previous_commute_not_finished')
            continue
        if len(state['people']) >= min(5000, state['max_people']) or released >= RELEASE_PER_STEP:
            break
        heapq.heappop(pending)
        workplace = registry['workplaces'][worker['workplace_id']]
        from .weather_response import office_allowed, begin
        if 'weather_response_outcomes' not in state: begin(state)
        if not office_allowed(state, worker, workplace, event):
            state['commute_totals']['weather_suppressed'] = state['commute_totals'].get('weather_suppressed', 0) + 1
            _record(state, worker, event, 'weather_suppressed', reason='Shared district weather attendance decision')
            continue
        legs, _, fallback = itinerary(worker, workplace, event[4], state['t'])
        # One stable person identity; outbound/return trip identities remain distinct.
        state['next_person'] += 1
        trip_id = f'{state["run_id"]}:commute:{state["next_person"]}'
        person = {'id': 'office-person:' + hashlib.sha256(worker['worker_id'].encode()).hexdigest()[:20],
            'trip_id': trip_id, 'office_worker_id': worker['worker_id'], 'workplace_id': worker['workplace_id'],
            'business_id': worker.get('business_id'), 'commute_direction': event[4], 'role_group': worker['role_group'],
            'commute_event': event, 'commute_legs': legs, 'commute_leg': 0, 'requested_mode': worker['commute_mode'],
            'fallback_reason': fallback, 'created_at': state['t'], 'departed_at': iso(state['t']),
            'source_sim_time': worker.get('source_sim_time'), 'wait_seconds': 0.0, 'event_id': None,
            'shared_vehicle_seconds': 0.0, 'co_passenger_seconds': 0.0, 'crowding_seconds': 0.0}
        _begin_leg(person, legs[0])
        state['people'].append(person); state['generated_people'] += 1; state['demand_requested'] += 1
        state['commute_totals']['materialized'] += 1
        state['commute_active_workers'][worker['worker_id']] = trip_id
        if event[4] == 'return':
            state['commute_at_work'].pop(worker['worker_id'], None)
        released += 1


def current_path(person):
    leg = person['commute_legs'][person['commute_leg']]
    return coordinate_path(tuple(leg['origin_xy']), tuple(leg['destination_xy']))


def finish_leg(state, person):
    """False means another leg continues; True permits normal trip completion."""
    leg = person['commute_legs'][person['commute_leg']]
    state['commute_leg_history'].append({'worker_id': person['office_worker_id'], 'workplace_id': person['workplace_id'],
        'business_id': person.get('business_id'), 'trip_id': person['trip_id'], 'direction': person['commute_direction'],
        'leg_index': person['commute_leg'], 'mode': leg['mode'], 'origin_id': leg['origin_id'],
        'destination_id': leg['destination_id'], 'completed_at': iso(state['t']),
        'source_sim_time': person.get('source_sim_time'), 'clock_policy': CLOCK_POLICY})
    if len(state['commute_leg_history']) > HISTORY_LIMIT:
        del state['commute_leg_history'][:-HISTORY_LIMIT]
    person['commute_leg'] += 1
    if person['commute_leg'] < len(person['commute_legs']):
        _begin_leg(person, person['commute_legs'][person['commute_leg']])
        return False
    event = person['commute_event']; worker_id = person['office_worker_id']
    state['commute_active_workers'].pop(worker_id, None)
    state['commute_totals']['completed'] += 1
    state['commute_totals'][person['commute_direction'] + '_completed'] += 1
    worker = {'worker_id': worker_id, 'workplace_id': person['workplace_id'],
              'business_id': person.get('business_id'), 'source_sim_time': person.get('source_sim_time')}
    _record(state, worker, event, 'completed', trip_id=person['trip_id'], person_id=person['id'],
            departed_at=person['departed_at'], arrived_at=iso(state['t']),
            requested_mode=person['requested_mode'], fallback_reason=person['fallback_reason'],
            shared_vehicle_seconds=round(person['shared_vehicle_seconds'], 3),
            co_passenger_seconds=round(person['co_passenger_seconds'], 3),
            crowding_seconds=round(person['crowding_seconds'], 3))
    if person['commute_direction'] == 'outbound':
        state['commute_at_work'][worker_id] = {'workplace_id': person['workplace_id'],
            'arrived_at': iso(state['t']), 'shift_end': event[3], 'business_id': person.get('business_id')}
        heapq.heappush(state['commute_events'], [max(state['t'], event[3]), worker_id, event[2], event[3], 'return'])
    return True


def observe_contacts(state, dt):
    vehicles = {v['id']: v for v in state['vehicles']}
    day = datetime.fromtimestamp(state['t'], TZ).date().isoformat()
    for person in state['people']:
        if not person.get('office_worker_id') or person['status'] != 'onboard':
            continue
        vehicle = vehicles[person['vehicle_id']]
        co_seconds = dt * max(0, vehicle['occupancy'] - 1)
        crowding = dt * vehicle['occupancy'] / max(1, vehicle['capacity'])
        person['shared_vehicle_seconds'] += dt; person['co_passenger_seconds'] += co_seconds; person['crowding_seconds'] += crowding
        key = person['workplace_id'] + ':' + day
        aggregate = state['commute_contacts'].setdefault(key, {'workplace_id': person['workplace_id'], 'date': day,
            'timezone': 'America/Moncton', 'passenger_seconds': 0.0, 'co_passenger_seconds': 0.0,
            'capacity_fraction_seconds': 0.0, 'clock_policy': CLOCK_POLICY,
            'model_note': 'Summed passenger contact-time proxy, not unique contacts or infections.'})
        aggregate['passenger_seconds'] += dt; aggregate['co_passenger_seconds'] += co_seconds; aggregate['capacity_fraction_seconds'] += crowding
    cutoff = (datetime.fromtimestamp(state['t'], TZ).date() - timedelta(days=13)).isoformat()
    state['commute_contacts'] = {k: v for k, v in state['commute_contacts'].items() if v['date'] >= cutoff}


def summary(state):
    totals = state.get('commute_totals', {})
    active = [p for p in state['people'] if p.get('office_worker_id')]
    return {'synthetic': True, 'clock_policy': CLOCK_POLICY, 'office_occupancy_policy': 'independent_source_schedule',
        'note': 'Office occupancy is not gated by these arrivals because source and Transit clocks are independent.',
        'registered_workplaces': len(state.get('commute_workplaces', [])),
        'registered_workers': sum(w['registered_workers'] for w in state.get('commute_workplaces', [])),
        'active': len(active), 'at_work_after_observed_arrival': len(state.get('commute_at_work', {})),
        'deferred': len(state.get('commute_pending', [])), 'scheduled': len(state.get('commute_events', [])),
        'active_cap': min(5000, state['max_people']), **totals,
        'outbound_active': sum(p['commute_direction'] == 'outbound' for p in active),
        'return_active': sum(p['commute_direction'] == 'return' for p in active),
        'contact_exposure': list(state.get('commute_contacts', {}).values())}
