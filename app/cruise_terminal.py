"""Cruise ship terminal operations layer (additive MVP).

One port (the shared northstar_harbour anchor), one terminal building, the
scheduled cruise calls from the engine arrivals timetable, passenger groups,
and three authored city destinations. Groups clear customs, choose a transport
mode (walk, terminal shuttle or taxi), visit a destination, return, clear
security and board before the ship departs.

Determinism and boundaries:
- A separate xorshift RNG stream lives in state['cruise_terminal']['rng'],
  seeded from the run seed. The main engine RNG stream is never consumed, so
  adding this layer does not change existing engine behavior or histories.
- Groups are the modeling unit, not individuals. The active group list is
  capped; finished groups collapse into per-visit and cumulative totals.
- This is a separate bounded operational layer: group counts here must not be
  added to the engine's individual traveler sample or to road cohorts.
- Group positions interpolate along real street geometry from the shared
  catalog graph; queue positions sit at the terminal anchor. Leg speeds are
  frozen at departure as authored proxies; this layer does not board transit
  vehicles or alter the engine's passenger accounting.
"""
from datetime import datetime
from math import hypot
from zoneinfo import ZoneInfo

from . import environment_effects
from .catalog import PLACE_INDEX, road_path

TZ = ZoneInfo('America/Moncton')
PORT_PLACE = 'northstar_harbour'
PORT_XY = (PLACE_INDEX[PORT_PLACE]['x'], PLACE_INDEX[PORT_PLACE]['y'])

# Authored MVP destinations within reach of the harbour, with occupancy caps.
DESTINATIONS = {
    'harbour_market': {'name': 'Harbour Market & Boardwalk', 'capacity': 900,
                       'dwell_minutes': (30, 90),
                       'appeal': {'food': 1.5, 'shopping': 1.3, 'culture': 1.0, 'nature': 0.6}},
    'marina': {'name': 'Cove Marina', 'capacity': 400, 'dwell_minutes': (25, 70),
               'appeal': {'food': 0.9, 'shopping': 0.7, 'culture': 1.3, 'nature': 1.1}},
    'coastal_beach': {'name': 'Bay Reach Beach', 'capacity': 600, 'dwell_minutes': (40, 110),
                      'appeal': {'food': 0.7, 'shopping': 0.5, 'culture': 0.6, 'nature': 1.6}},
}
INTERESTS = ('food', 'shopping', 'culture', 'nature')
WALK_LIMIT_M = 1800
WALK_SPEED = 1.35
REDUCED_WALK_SPEED = 0.9
SHUTTLE_SPEED = 8.0
TAXI_SPEED = 11.0
GROUP_SIZES = (1, 2, 3, 4, 5, 6)
GROUP_SIZE_WEIGHTS = (0.15, 0.25, 0.20, 0.20, 0.12, 0.08)
MAX_EVENTS = 120
MAX_HISTORY = 360
DETAIL_GROUP_LIMIT = 250

DEFAULT_CONFIG = {
    'berths': 2, 'customs_lanes': 4, 'security_lanes': 4,
    'customs_seconds_per_group': 45, 'security_seconds_per_group': 30,
    'shuttle_capacity_people': 50, 'shuttle_headway_minutes': 15,
    'taxi_interval_seconds': 40, 'shore_minutes': 330, 'boarding_minutes': 60,
    'group_cap': 600,
}
CONFIG_LIMITS = {'berths': (1, 3), 'customs_lanes': (1, 12), 'security_lanes': (1, 12),
                 'customs_seconds_per_group': (10, 180), 'security_seconds_per_group': (10, 120),
                 'shuttle_capacity_people': (0, 120), 'shuttle_headway_minutes': (5, 60),
                 'taxi_interval_seconds': (10, 240), 'shore_minutes': (60, 720),
                 'boarding_minutes': (20, 180), 'group_cap': (50, 2000)}

_GEOMETRY = {}


def _iso(seconds):
    return datetime.fromtimestamp(seconds, TZ).isoformat(timespec='seconds')


def _rand(terminal):
    """Deterministic xorshift stream owned by this layer (JSON round-trip safe)."""
    value = terminal['rng']
    value ^= (value << 13) & 0xffffffff
    value ^= value >> 17
    value ^= (value << 5) & 0xffffffff
    terminal['rng'] = value & 0xffffffff
    return terminal['rng'] / 4294967296


def _weighted(terminal, weighted_items):
    total = sum(weight for _, weight in weighted_items)
    if total <= 0:
        return weighted_items[0][0]
    threshold = _rand(terminal) * total
    for item, weight in weighted_items:
        threshold -= weight
        if threshold <= 0:
            return item
    return weighted_items[-1][0]


def _group_size(terminal):
    return _weighted(terminal, list(zip(GROUP_SIZES, GROUP_SIZE_WEIGHTS)))


def _log(terminal, state, kind, text):
    terminal['events'].append({'time': _iso(state['t']), 'kind': kind, 'text': text})
    del terminal['events'][:-MAX_EVENTS]


def _travel_time_multiplier(state):
    try:
        value = environment_effects.multiplier(state, 'travel_time')
    except Exception:
        return 1.0
    return value if isinstance(value, (int, float)) and 0.2 <= value <= 5.0 else 1.0


def _demand_multiplier(state):
    try:
        value = environment_effects.multiplier(state, 'demand')
    except Exception:
        return 1.0
    return value if isinstance(value, (int, float)) and 0.2 <= value <= 5.0 else 1.0


def _geometry(destination_id):
    """Street geometry from the shared catalog graph, cached per process."""
    key = (PORT_PLACE, destination_id)
    if key not in _GEOMETRY:
        try:
            path = road_path(PORT_PLACE, destination_id)
            points = [list(point) for point in path['points']]
            length = float(path['length'])
        except Exception:
            x, y = PLACE_INDEX[destination_id]['x'], PLACE_INDEX[destination_id]['y']
            points = [list(PORT_XY), [x, y]]
            length = hypot(x - PORT_XY[0], y - PORT_XY[1]) * 1.3
        _GEOMETRY[key] = (points, max(1.0, length))
    return _GEOMETRY[key]


def _position(points, distance, from_end=False):
    remaining = max(0.0, distance)
    sequence = points if not from_end else list(reversed(points))
    for (x1, y1), (x2, y2) in zip(sequence, sequence[1:]):
        segment = hypot(x2 - x1, y2 - y1)
        if segment <= 0:
            continue
        if remaining <= segment:
            fraction = remaining / segment
            return x1 + (x2 - x1) * fraction, y1 + (y2 - y1) * fraction
        remaining -= segment
    return sequence[-1][0], sequence[-1][1]


def ensure_cruise_terminal(state):
    terminal = state.get('cruise_terminal')
    if terminal is None:
        terminal = state['cruise_terminal'] = {
            'schema_version': 1,
            'rng': ((int(state.get('seed', 42)) ^ 0x9e3779b9) & 0xffffffff) or 1,
            'config': dict(DEFAULT_CONFIG),
            'visits': [], 'groups': [], 'events': [], 'history': [],
            'customs_carry': 0.0, 'security_carry': 0.0, 'taxi_carry': 0.0,
            'next_shuttle_ts': state['t'] + DEFAULT_CONFIG['shuttle_headway_minutes'] * 60,
            'next_group': 0, 'suppressed_people': 0, 'last_history_t': state['t'],
            'totals': {'groups_created': 0, 'people_disembarked': 0, 'people_boarded': 0,
                       'people_missed': 0, 'spend': 0.0,
                       'visitors_by_destination': {}, 'spend_by_destination': {}},
        }
        _sync_visits(state, terminal)
    return terminal


def apply_scenario(state, **updates):
    """Validate and store terminal scenario settings; affects visits created afterwards."""
    terminal = ensure_cruise_terminal(state)
    for key, value in updates.items():
        if key not in DEFAULT_CONFIG:
            raise ValueError(f'Unknown cruise terminal setting: {key}')
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f'{key} must be an integer')
        low, high = CONFIG_LIMITS[key]
        if not low <= value <= high:
            raise ValueError(f'{key} must be between {low} and {high}')
    terminal['config'].update(updates)
    if updates:
        _log(terminal, state, 'scenario',
             'Terminal scenario updated: ' + ', '.join(f'{key}={value}' for key, value in updates.items()))
    return cruise_terminal_detail(state)


def _assign_berth(terminal):
    used = {visit['berth'] for visit in terminal['visits']
            if visit['berth'] is not None and visit['status'] != 'departed'}
    for berth in range(1, terminal['config']['berths'] + 1):
        if berth not in used:
            return berth
    return None


def _visit_for(state, terminal, arrival):
    config = terminal['config']
    arrival_ts = arrival['timestamp']
    duration = max(60, int(arrival.get('duration_seconds') or 2400))
    departure_ts = arrival_ts + duration + config['shore_minutes'] * 60 + config['boarding_minutes'] * 60
    visit = {'id': arrival['id'], 'ship': arrival['name'], 'manifest': arrival['passengers'],
             'modeled': arrival['modeled_passengers'], 'arrival_ts': arrival_ts,
             'arrival_iso': arrival['time'], 'departure_ts': departure_ts,
             'duration_seconds': duration, 'berth': None, 'berth_ts': None,
             'disembark_end_ts': arrival_ts + duration,
             'boarding_start_ts': departure_ts - config['boarding_minutes'] * 60,
             'status': 'scheduled', 'released_people': 0, 'pending_people': 0.0,
             'disembarked_people': 0, 'boarded_people': 0, 'missed_people': 0,
             'spend': 0.0, 'note': None}
    if state['t'] >= departure_ts:
        visit['status'] = 'departed'
        visit['note'] = 'Run started after this call ended; the historical call was not re-simulated.'
    _log(terminal, state, 'ship', f"{visit['ship']} scheduled: berth window "
         f"{_iso(arrival_ts)}–{_iso(departure_ts)}, manifest {visit['manifest']}.")
    return visit


def _sync_visits(state, terminal):
    known = {visit['id'] for visit in terminal['visits']}
    for arrival in state.get('arrivals', []):
        if arrival.get('mode') != 'cruise' or arrival.get('place_id') != PORT_PLACE or arrival['id'] in known:
            continue
        terminal['visits'].append(_visit_for(state, terminal, arrival))
    terminal['visits'].sort(key=lambda visit: visit['arrival_ts'])
    for visit in terminal['visits']:
        if visit['status'] == 'scheduled' and state['t'] >= visit['arrival_ts']:
            berth = _assign_berth(terminal)
            if berth is None:
                visit['status'] = 'holding'
                _log(terminal, state, 'berth', f"{visit['ship']} holds outside the harbour: all berths occupied.")
            else:
                visit['berth'] = berth
                visit['berth_ts'] = state['t']
                visit['status'] = 'disembarking'
                _log(terminal, state, 'berth', f"{visit['ship']} berthed at berth {berth}; disembarkation begins.")
        elif visit['status'] == 'holding':
            berth = _assign_berth(terminal)
            if berth is not None:
                visit['berth'] = berth
                visit['berth_ts'] = state['t']
                visit['status'] = 'disembarking'
                _log(terminal, state, 'berth', f"{visit['ship']} took berth {berth} after holding; disembarkation begins.")
        if visit['status'] == 'disembarking' and state['t'] >= visit['disembark_end_ts']:
            visit['status'] = 'ashore'
            _log(terminal, state, 'ship', f"{visit['ship']} disembarkation complete; shore leave in progress.")
        if visit['status'] == 'ashore' and state['t'] >= visit['boarding_start_ts']:
            visit['status'] = 'boarding'
            _log(terminal, state, 'ship', f"{visit['ship']} boarding window opened; security clearance required.")
        if visit['status'] in ('disembarking', 'ashore', 'boarding') and state['t'] >= visit['departure_ts']:
            _finalize_departure(state, terminal, visit)


def _release_groups(state, terminal, visit):
    """Release modeled people down the gangway over the disembarkation window."""
    if visit['status'] != 'disembarking' or visit['berth_ts'] is None:
        return
    elapsed = max(0.0, state['t'] - visit['berth_ts'])
    target = min(float(visit['modeled']), visit['modeled'] * elapsed / max(1.0, visit['duration_seconds']))
    visit['pending_people'] += max(0.0, target - visit['released_people'])
    visit['released_people'] = min(target, visit['modeled'])
    while visit['pending_people'] >= 1.0 and len(terminal['groups']) < terminal['config']['group_cap']:
        size = min(int(visit['pending_people']), _group_size(terminal) or 1)
        if size <= 0:
            break
        _create_group(state, terminal, visit, size)
        visit['pending_people'] -= size
    if len(terminal['groups']) >= terminal['config']['group_cap'] and visit['pending_people'] >= 1.0:
        suppressed = int(visit['pending_people'])
        terminal['suppressed_people'] += suppressed
        visit['pending_people'] = 0.0
        visit['released_people'] = visit['modeled']
        _log(terminal, state, 'capacity',
             f"Group sample cap reached; {suppressed} later arrivals from {visit['ship']} were not modeled as groups.")


def _create_group(state, terminal, visit, size, pre_cleared=False):
    sequence = terminal['next_group']
    terminal['next_group'] += 1
    reduced = _rand(terminal) < 0.12
    interests = _weighted_interests(terminal)
    group = {'id': f"{visit['id']}:{sequence:04d}", 'visit': visit['id'], 'size': size,
             'mobility_reduced': reduced, 'budget': int(40 + _rand(terminal) * 140),
             'interests': interests, 'dest': None, 'mode': None, 'state': 'customs_queue',
             'leg': None, 'distance': 0.0, 'progress': 0.0, 'speed': 0.0,
             'dwell_left': 0.0, 'return_at': None, 'delay_left': 0.0,
             'x': PORT_XY[0] - 70 + (sequence % 12) * 12, 'y': PORT_XY[1] - 30 - (sequence % 5) * 14}
    terminal['groups'].append(group)
    terminal['totals']['groups_created'] += 1
    visit['disembarked_people'] += size
    terminal['totals']['people_disembarked'] += size
    if pre_cleared:
        _depart_customs(state, terminal, visit, group)


def _weighted_interests(terminal):
    first = INTERESTS[int(_rand(terminal) * len(INTERESTS)) % len(INTERESTS)]
    second = INTERESTS[int(_rand(terminal) * len(INTERESTS)) % len(INTERESTS)]
    return [first] if first == second else [first, second]


def _occupancy(terminal, destination_id):
    return sum(group['size'] for group in terminal['groups']
               if group['dest'] == destination_id and group['state'] in ('at_destination', 'overflow_wait'))


def _choose_destination(state, terminal, group):
    demand = _demand_multiplier(state)
    weighted = []
    for destination_id, destination in DESTINATIONS.items():
        appeal = sum(destination['appeal'][interest] for interest in group['interests'])
        if 'nature' in group['interests']:
            appeal *= demand  # Poor weather suppresses outdoor appeal (authored proxy).
        headroom = max(0.15, 1.0 - _occupancy(terminal, destination_id) / destination['capacity'])
        weighted.append((destination_id, max(0.05, appeal * headroom)))
    return _weighted(terminal, weighted)


def _choose_mode(state, terminal, group, destination_id):
    _, distance = _geometry(destination_id)
    config = terminal['config']
    if group['mobility_reduced']:
        if distance <= 400 and _rand(terminal) < 0.5:
            return 'walk'
        if config['shuttle_capacity_people'] > 0 and _rand(terminal) < 0.6:
            return 'shuttle'
        return 'taxi'
    if distance <= WALK_LIMIT_M and _rand(terminal) < 0.55:
        return 'walk'
    if group['budget'] >= 120 and _rand(terminal) < 0.5:
        return 'taxi'
    if config['shuttle_capacity_people'] > 0 and _rand(terminal) < 0.6:
        return 'shuttle'
    return 'taxi'


def _mode_speed(state, group):
    if group['mode'] == 'walk':
        return REDUCED_WALK_SPEED if group['mobility_reduced'] else WALK_SPEED
    congestion = max(0.0, min(95.0, state.get('congestion_pct', 0.0)))
    factor = max(0.35, 1.0 - congestion / 100.0 * 0.6) * _travel_time_multiplier(state)
    return (SHUTTLE_SPEED if group['mode'] == 'shuttle' else TAXI_SPEED) * factor


def _start_leg(state, terminal, group, leg):
    points, distance = _geometry(group['dest'])
    group['leg'] = leg
    group['state'] = 'transit'
    group['distance'] = distance
    group['progress'] = 0.0
    group['speed'] = _mode_speed(state, group)
    x, y = _position(points, 0.0, from_end=(leg == 'return'))
    group['x'], group['y'] = x, y


def _depart_customs(state, terminal, visit, group):
    group['dest'] = _choose_destination(state, terminal, group)
    group['mode'] = _choose_mode(state, terminal, group, group['dest'])
    if group['mode'] == 'walk':
        _start_leg(state, terminal, group, 'outbound')
    elif group['mode'] == 'shuttle':
        group['state'] = 'shuttle_queue'
        group['x'], group['y'] = PORT_XY[0] + 60, PORT_XY[1] + 20
    else:
        group['state'] = 'taxi_queue'
        group['x'], group['y'] = PORT_XY[0] + 90, PORT_XY[1] - 10


def _arrive_destination(state, terminal, visit, group):
    destination = DESTINATIONS[group['dest']]
    if _occupancy(terminal, group['dest']) + group['size'] > destination['capacity']:
        group['state'] = 'overflow_wait'
        return
    _enter_destination(state, terminal, visit, group)


def _enter_destination(state, terminal, visit, group):
    destination = DESTINATIONS[group['dest']]
    low, high = destination['dwell_minutes']
    group['state'] = 'at_destination'
    group['dwell_left'] = (low + _rand(terminal) * (high - low)) * 60
    place = PLACE_INDEX[group['dest']]
    slot = int(group['id'][-4:])
    group['x'] = place['x'] + (slot % 7) * 8 - 24
    group['y'] = place['y'] + ((slot // 7) % 7) * 8 - 24
    spend = round(group['size'] * group['budget'] * (0.5 + 0.9 * _rand(terminal)), 2)
    group['spend'] = spend
    visit['spend'] = round(visit['spend'] + spend, 2)
    terminal['totals']['spend'] = round(terminal['totals']['spend'] + spend, 2)
    terminal['totals']['spend_by_destination'][group['dest']] = round(
        terminal['totals']['spend_by_destination'].get(group['dest'], 0.0) + spend, 2)
    terminal['totals']['visitors_by_destination'][group['dest']] = (
        terminal['totals']['visitors_by_destination'].get(group['dest'], 0) + group['size'])


def _begin_return(state, terminal, visit, group):
    if _rand(terminal) < 0.08:  # Authored chance of a delayed party.
        group['delay_left'] = (15 + 25 * _rand(terminal)) * 60
    slack = (10 + 90 * _rand(terminal)) * 60 + (1800 if group['mobility_reduced'] else 0)
    group['return_at'] = min(state['t'], visit['boarding_start_ts'] - slack)


def _advance_groups(state, terminal, dt):
    for group in terminal['groups']:
        visit = _visit_for_id(terminal, group['visit'])
        if visit is None or visit['status'] == 'departed':
            continue
        if group['state'] == 'transit':
            group['progress'] += dt * group['speed']
            points, _ = _geometry(group['dest'])
            x, y = _position(points, group['progress'], from_end=(group['leg'] == 'return'))
            group['x'], group['y'] = x, y
            if group['progress'] >= group['distance']:
                if group['leg'] == 'outbound':
                    _arrive_destination(state, terminal, visit, group)
                else:
                    group['state'] = 'security_queue'
                    group['x'], group['y'] = PORT_XY[0] + 40, PORT_XY[1] - 50
        elif group['state'] == 'at_destination':
            group['dwell_left'] -= dt
            if group['dwell_left'] <= 0 and group['return_at'] is None:
                _begin_return(state, terminal, visit, group)
            if group['return_at'] is not None and state['t'] >= group['return_at']:
                if group['delay_left'] > 0:
                    group['delay_left'] -= dt
                else:
                    _start_leg(state, terminal, group, 'return')
        elif group['state'] == 'overflow_wait':
            if _occupancy(terminal, group['dest']) + group['size'] <= DESTINATIONS[group['dest']]['capacity']:
                _enter_destination(state, terminal, visit, group)


def _visit_for_id(terminal, visit_id):
    for visit in terminal['visits']:
        if visit['id'] == visit_id:
            return visit
    return None


def _serve_queues(state, terminal, dt):
    config = terminal['config']
    customs = [group for group in terminal['groups'] if group['state'] == 'customs_queue']
    security = [group for group in terminal['groups'] if group['state'] == 'security_queue']
    terminal['customs_carry'] += config['customs_lanes'] * dt / max(1, config['customs_seconds_per_group'])
    while terminal['customs_carry'] >= 1.0 and customs:
        group = customs.pop(0)
        visit = _visit_for_id(terminal, group['visit'])
        if visit is None:
            break
        _depart_customs(state, terminal, visit, group)
        terminal['customs_carry'] -= 1.0
    if not customs:
        terminal['customs_carry'] = min(terminal['customs_carry'], 1.0)
    terminal['security_carry'] += config['security_lanes'] * dt / max(1, config['security_seconds_per_group'])
    while terminal['security_carry'] >= 1.0 and security:
        group = security.pop(0)
        visit = _visit_for_id(terminal, group['visit'])
        if visit is None:
            break
        visit['boarded_people'] += group['size']
        terminal['totals']['people_boarded'] += group['size']
        terminal['groups'].remove(group)
        terminal['security_carry'] -= 1.0
    if not security:
        terminal['security_carry'] = min(terminal['security_carry'], 1.0)
    interval = config['taxi_interval_seconds'] * (1 + max(0.0, min(95.0, state.get('congestion_pct', 0.0))) / 100.0 * 1.5) \
        * _travel_time_multiplier(state)
    taxis = [group for group in terminal['groups'] if group['state'] == 'taxi_queue']
    terminal['taxi_carry'] += dt / max(1.0, interval)
    while terminal['taxi_carry'] >= 1.0 and taxis:
        group = taxis.pop(0)
        _start_leg(state, terminal, group, 'outbound')
        terminal['taxi_carry'] -= 1.0
    if not taxis:
        terminal['taxi_carry'] = min(terminal['taxi_carry'], 1.0)
    _serve_shuttles(state, terminal, dt)


def _serve_shuttles(state, terminal, dt):
    config = terminal['config']
    if config['shuttle_capacity_people'] <= 0:
        return
    if state['t'] >= terminal['next_shuttle_ts']:
        waiting = [group for group in terminal['groups'] if group['state'] == 'shuttle_queue']
        remaining = config['shuttle_capacity_people']
        for group in waiting:
            if group['size'] <= remaining:
                _start_leg(state, terminal, group, 'outbound')
                remaining -= group['size']
        terminal['next_shuttle_ts'] = max(terminal['next_shuttle_ts'] + config['shuttle_headway_minutes'] * 60,
                                          state['t'] + config['shuttle_headway_minutes'] * 60)


def _finalize_departure(state, terminal, visit):
    stragglers = [group for group in terminal['groups'] if group['visit'] == visit['id']]
    missed_people = sum(group['size'] for group in stragglers)
    for group in stragglers:
        terminal['groups'].remove(group)
    visit['missed_people'] += missed_people
    terminal['totals']['people_missed'] += missed_people
    visit['status'] = 'departed'
    if missed_people:
        _log(terminal, state, 'missed',
             f"{visit['ship']} departed with {missed_people} modeled passengers not yet aboard.")
    else:
        _log(terminal, state, 'ship', f"{visit['ship']} departed with all modeled passengers aboard.")


def _prune(state, terminal):
    terminal['visits'] = [visit for visit in terminal['visits']
                          if visit['status'] != 'departed' or visit['departure_ts'] + 6 * 3600 > state['t']][-8:]
    known = {visit['id'] for visit in terminal['visits']}
    terminal['groups'] = [group for group in terminal['groups'] if group['visit'] in known]


def _at_risk_people(state, terminal, visit):
    config = terminal['config']
    security_ahead = sum(group['size'] for group in terminal['groups'] if group['state'] == 'security_queue')
    seconds_per_group = max(1, config['security_seconds_per_group']) / max(1, config['security_lanes'])
    at_risk = 0
    for group in terminal['groups']:
        if group['visit'] != visit['id'] or group['state'] == 'security_queue':
            continue
        if group['state'] == 'transit' and group['speed'] > 0:
            remaining_travel = max(0.0, group['distance'] - group['progress']) / group['speed']
        elif group['state'] == 'at_destination':
            remaining_travel = 600.0
        elif group['state'] in ('customs_queue', 'taxi_queue', 'shuttle_queue'):
            remaining_travel = 900.0  # Authored proxy: still faces transport and return legs.
        else:
            remaining_travel = 0.0
        eta = remaining_travel + security_ahead * seconds_per_group
        if state['t'] + eta > visit['departure_ts'] - 900:
            at_risk += group['size']
    return at_risk


def _record_history(state, terminal):
    terminal['last_history_t'] = state['t']
    ashore = sum(group['size'] for group in terminal['groups'])
    terminal['history'].append({'time': _iso(state['t']), 'time_ts': state['t'], 'ashore_people': ashore,
                                'customs_queue': sum(group['size'] for group in terminal['groups']
                                                     if group['state'] == 'customs_queue'),
                                'security_queue': sum(group['size'] for group in terminal['groups']
                                                      if group['state'] == 'security_queue'),
                                'occupancy_people': sum(_occupancy(terminal, dest) for dest in DESTINATIONS),
                                'disembarked_total': terminal['totals']['people_disembarked'],
                                'boarded_total': terminal['totals']['people_boarded']})
    del terminal['history'][:-MAX_HISTORY]


def advance_cruise_terminal(state, dt):
    terminal = ensure_cruise_terminal(state)
    _sync_visits(state, terminal)
    for visit in terminal['visits']:
        _release_groups(state, terminal, visit)
    _advance_groups(state, terminal, dt)
    _serve_queues(state, terminal, dt)
    _prune(state, terminal)
    if state['t'] - terminal['last_history_t'] >= 60:
        _record_history(state, terminal)


def _visit_summary(state, terminal, visit):
    ashore = sum(group['size'] for group in terminal['groups'] if group['visit'] == visit['id'])
    boarded = visit['boarded_people']
    missed = visit['missed_people']
    completed = boarded + missed
    return {'id': visit['id'], 'ship': visit['ship'], 'berth': visit['berth'], 'status': visit['status'],
            'manifest': visit['manifest'], 'modeled': visit['modeled'],
            'arrival_iso': visit['arrival_iso'], 'departure_iso': _iso(visit['departure_ts']),
            'disembarked_people': visit['disembarked_people'], 'ashore_people': ashore,
            'boarded_people': boarded, 'missed_people': missed,
            'at_risk_people': _at_risk_people(state, terminal, visit) if visit['status'] != 'departed' else 0,
            'spend': visit['spend'], 'note': visit['note'],
            'return_rate_pct': round(100.0 * boarded / completed, 1) if completed else None}


def _throughput(state, terminal):
    """Disembark/boarding rates in people per hour over the recent history window."""
    history = terminal['history']
    if len(history) < 2:
        return {'disembarked_per_hour': None, 'boarded_per_hour': None, 'window_minutes': None}
    latest = history[-1]
    earlier = next((row for row in reversed(history) if state['t'] - row['time_ts'] <= 900), history[0])
    elapsed_minutes = max(1.0, (latest['time_ts'] - earlier['time_ts']) / 60.0)
    hours = elapsed_minutes / 60.0
    return {'disembarked_per_hour': round(max(0, latest['disembarked_total'] - earlier['disembarked_total']) / hours),
            'boarded_per_hour': round(max(0, latest['boarded_total'] - earlier['boarded_total']) / hours),
            'window_minutes': round(elapsed_minutes)}


def _insights(state, terminal, ships, queues, destinations):
    """Authored operational advisories for port authority dashboards.

    Every insight is derived from the terminal's own simulated state; nothing
    here is an external measurement or a prediction beyond the model.
    """
    config = terminal['config']
    insights = []
    active = [ship for ship in ships if ship['status'] != 'departed']
    for ship in active:
        minutes_to_departure = max(0, round((visit_departure(state, terminal, ship['id']) - state['t']) / 60)) \
            if ship['status'] != 'scheduled' else None
        if ship['status'] == 'holding':
            insights.append({'severity': 'alert', 'title': f"No berth available — {ship['ship']} holding",
                             'detail': 'All berths are occupied; the call cannot serve passengers until one frees.'})
        if ship['status'] == 'boarding' and ship['ashore_people'] > 0:
            insights.append({'severity': 'alert' if ship['at_risk_people'] > 0 else 'watch',
                             'title': f"All aboard {ship['ship']} — departs in {minutes_to_departure} min",
                             'detail': f"{ship['ashore_people']} passengers still ashore, "
                                       f"{ship['at_risk_people']} at risk of missing departure."})
        elif ship['at_risk_people'] > 0 and minutes_to_departure is not None and minutes_to_departure <= 90:
            insights.append({'severity': 'watch', 'title': f"Return risk building for {ship['ship']}",
                             'detail': f"{ship['at_risk_people']} passengers may not reach boarding in time."})
    customs_wait = queues['customs_groups'] * config['customs_seconds_per_group'] / max(1, config['customs_lanes']) / 60
    security_wait = queues['security_groups'] * config['security_seconds_per_group'] / max(1, config['security_lanes']) / 60
    if customs_wait >= 8:
        insights.append({'severity': 'alert' if customs_wait >= 15 else 'watch',
                         'title': f"Customs queue ≈{customs_wait:.0f} min",
                         'detail': f"{queues['customs_people']} passengers waiting across {config['customs_lanes']} lanes."})
    if security_wait >= 8:
        insights.append({'severity': 'alert' if security_wait >= 15 else 'watch',
                         'title': f"Security queue ≈{security_wait:.0f} min",
                         'detail': f"{queues['security_people']} passengers waiting across {config['security_lanes']} lanes."})
    if queues['shuttle_queue_people'] > 2 * max(1, config['shuttle_capacity_people']):
        insights.append({'severity': 'watch', 'title': 'Shuttle demand exceeds capacity',
                         'detail': f"{queues['shuttle_queue_people']} passengers waiting; next shuttle {_iso(terminal['next_shuttle_ts'])[11:16]}."})
    berthed = [ship for ship in active if ship['berth'] is not None]
    if len(berthed) >= 2:
        insights.append({'severity': 'info', 'title': f"Multi-ship day — {len(berthed)} vessels in port",
                         'detail': 'Berth occupancy is at its limit; expect peak terminal and road demand.'})
    for destination in destinations:
        if destination['capacity'] and destination['occupancy_people'] / destination['capacity'] >= 0.9:
            insights.append({'severity': 'watch', 'title': f"{destination['name']} nearing capacity",
                             'detail': f"{destination['occupancy_people']} of {destination['capacity']} visitor spaces in use."})
    if not insights:
        if active:
            insights.append({'severity': 'info', 'title': 'Terminal operations nominal',
                             'detail': f"{active[0]['ship']}: no congestion or all-aboard risks detected."})
        else:
            next_call = min((visit for visit in terminal['visits'] if visit['status'] == 'scheduled'),
                            key=lambda visit: visit['arrival_ts'], default=None)
            insights.append({'severity': 'info', 'title': 'No ship in port',
                             'detail': f"Next scheduled call: {next_call['ship']} at {next_call['arrival_iso'][11:16]}."
                             if next_call else 'No cruise calls are scheduled for the visible window.'})
    order = {'alert': 0, 'watch': 1, 'info': 2}
    return sorted(insights, key=lambda insight: order[insight['severity']])[:8]


def visit_departure(state, terminal, visit_id):
    visit = _visit_for_id(terminal, visit_id)
    return visit['departure_ts'] if visit else state['t']


def cruise_terminal_summary(state):
    terminal = state.get('cruise_terminal')
    if not terminal:
        return {'enabled': False, 'reason': 'not_initialized'}
    config = terminal['config']
    groups = terminal['groups']
    queues = {'customs_groups': sum(group['state'] == 'customs_queue' for group in groups),
              'customs_people': sum(group['size'] for group in groups if group['state'] == 'customs_queue'),
              'security_groups': sum(group['state'] == 'security_queue' for group in groups),
              'security_people': sum(group['size'] for group in groups if group['state'] == 'security_queue'),
              'taxi_queue_groups': sum(group['state'] == 'taxi_queue' for group in groups),
              'shuttle_queue_groups': sum(group['state'] == 'shuttle_queue' for group in groups),
              'shuttle_queue_people': sum(group['size'] for group in groups if group['state'] == 'shuttle_queue'),
              'next_shuttle_iso': _iso(terminal['next_shuttle_ts'])}
    destinations = []
    for destination_id, destination in DESTINATIONS.items():
        destinations.append({'id': destination_id, 'name': destination['name'], 'capacity': destination['capacity'],
                             'occupancy_people': _occupancy(terminal, destination_id),
                             'waiting_people': sum(group['size'] for group in groups
                                                   if group['dest'] == destination_id and group['state'] == 'overflow_wait'),
                             'visitors': terminal['totals']['visitors_by_destination'].get(destination_id, 0),
                             'spend': terminal['totals']['spend_by_destination'].get(destination_id, 0.0)})
    totals = dict(terminal['totals'])
    completed = totals['people_boarded'] + totals['people_missed']
    totals['return_rate_pct'] = round(100.0 * totals['people_boarded'] / completed, 1) if completed else None
    ships = [_visit_summary(state, terminal, visit) for visit in terminal['visits']]
    imminent = next((ship for ship in ships if ship['status'] not in ('departed', 'scheduled')), None) \
        or next((ship for ship in ships if ship['status'] == 'scheduled'), None)
    all_aboard = None
    if imminent and imminent['status'] != 'scheduled':
        visit = _visit_for_id(terminal, imminent['id'])
        all_aboard = {'ship': imminent['ship'], 'status': imminent['status'], 'berth': imminent['berth'],
                      'minutes_to_boarding_start': max(0, round((visit['boarding_start_ts'] - state['t']) / 60)),
                      'minutes_to_departure': max(0, round((visit['departure_ts'] - state['t']) / 60)),
                      'ashore_people': imminent['ashore_people'], 'boarded_people': imminent['boarded_people'],
                      'at_risk_people': imminent['at_risk_people']}
    return {'enabled': True, 'port': PORT_PLACE, 'synthetic': True,
            'terminal': 'Northstar Cruise Terminal', 'config': dict(config),
            'ships': ships,
            'queues': queues, 'destinations': destinations, 'totals': totals,
            'insights': _insights(state, terminal, ships, queues, destinations),
            'throughput': _throughput(state, terminal), 'all_aboard': all_aboard,
            'ashore_people': sum(group['size'] for group in groups),
            'suppressed_people': terminal['suppressed_people'],
            'model_scope': 'Group-level terminal operations; a separate bounded layer that must not be added to the traveler sample.'}


def cruise_terminal_detail(state):
    terminal = ensure_cruise_terminal(state)
    groups = [{'id': group['id'], 'size': group['size'], 'state': group['state'],
               'destination': group['dest'], 'mode': group['mode'], 'leg': group['leg'],
               'mobility_reduced': group['mobility_reduced'], 'x': round(group['x'], 1), 'y': round(group['y'], 1)}
              for group in terminal['groups'][:DETAIL_GROUP_LIMIT]]
    detail = cruise_terminal_summary(state)
    detail.update({'clock': {'iso': _iso(state['t'])}, 'groups': groups,
                   'events': list(reversed(terminal['events'][-60:])),
                   'history': list(terminal['history']),
                   'environment': {'travel_time': _travel_time_multiplier(state),
                                   'demand': _demand_multiplier(state)},
                   'active_group_count': len(terminal['groups'])})
    return detail


# ---------------------------------------------------------------------------
# Data-only 3D preview scene (northstar.preview.scene.v1).
# Coordinates are the shared northstar-local-m-v1 metres (x east, y north);
# scene position is [x, z] with z equal to the model's north coordinate and
# size is [width east-west, height, depth north-south]. Footprints, heights
# and ship dimensions are illustrative for preview, not surveyed dimensions.
# ---------------------------------------------------------------------------
SCENE_CONTRACT = 'northstar.preview.scene.v1'
SCENE_OBJECT_LIMIT = 40
SCENE_BERTH_Z = {1: 22100, 2: 22500, 3: 22900}
SCENE_SHIP_SIZES = {1: [60, 22, 280], 2: [55, 20, 260], 3: [50, 18, 240]}
SCENE_WHARF_X = 26850
SCENE_HOLDING_XY = [27500, 22500]
SCENE_STATUS_LABELS = {'disembarking': 'Disembarking', 'ashore': 'Shore leave', 'boarding': 'All aboard'}
SCENE_GEOMETRY_NOTE = (
    'Data-only preview scene; no external assets. Coordinates use the shared northstar-local-m-v1 '
    'grid in metres (x east, y north): scene position is [x, z] with z equal to the model north '
    'coordinate, and size is [width east-west, height, depth north-south]. The layout is authored '
    'around the shared harbour anchor (26500, 22500). Building footprints, heights, pier dimensions '
    'and ship sizes are illustrative for preview, not surveyed dimensions. Destination buildings sit '
    'at their real shared venue anchors, and cruise_route roads trace the actual street-graph paths '
    'between the harbour and each destination. The scene contract has no water kind, so the harbour '
    'channel is not drawn. Ships appear only when the simulator state has a berthed or holding call, '
    'and counts in details are modeled terminal state; no activity is invented.')
SCENE_AMENITIES = (
    ('cruise_checkin', 'Check-in & Reception Hall', [26280, 22500], [90, 12, 70],
     'Staffed check-in desks and reception for embarking passengers.'),
    ('cruise_customs', 'Customs & Immigration Hall', [26430, 22470], [70, 9, 40],
     'Arrival control lanes serving disembarking groups.'),
    ('cruise_security', 'Security Screening', [26430, 22530], [70, 9, 40],
     'Re-entry screening before the gangway on return.'),
    ('cruise_baggage', 'Baggage & Porter Services', [26350, 22465], [60, 8, 30],
     'Baggage claim and porter assistance.'),
    ('cruise_lounge', 'Waiting Lounge & Gates', [26350, 22535], [60, 8, 30],
     'Seating, all-aboard announcements and gate access.'),
    ('cruise_retail', 'Café, Retail & Duty-free', [26260, 22465], [50, 6, 28],
     'Food, drinks and duty-free shopping.'),
    ('cruise_info', 'Restrooms & Information Desk', [26260, 22535], [50, 6, 28],
     'Restrooms, information desk and accessibility services.'),
)
SCENE_GANGWAYS = (
    ('cruise_gangway_north', 'Gangway North', [26655, 22430], [350, 3, 10]),
    ('cruise_gangway_south', 'Gangway South', [26655, 22570], [350, 3, 10]),
)
SCENE_STAGING = (
    ('cruise_taxi_rank', 'Taxi Rank', [26140, 22430], [90, 3, 70],
     'Staged taxis serve groups at the modeled dispatch interval.'),
    ('cruise_shuttle_bay', 'Shuttle Bay', [26140, 22570], [90, 3, 70],
     'Staged terminal shuttle; capacity and headway come from the scenario config.'),
)
SCENE_ROADS = (
    ('cruise_terminal_access', [[25400, 22500], [25800, 22500], [26100, 22500]]),
    ('cruise_terminal_forecourt', [[26100, 22430], [26100, 22570]]),
    ('cruise_wharf_service_lane', [[26480, 22430], [26830, 22430]]),
)


def _scene_object(identifier, label, kind, position, size, details='', heading=None):
    entry = {'id': identifier, 'label': label, 'kind': kind,
             'position': [position[0], position[1]], 'size': list(size), 'details': details}
    if heading is not None:
        entry['heading'] = round(heading, 4)
    return entry


def cruise_terminal_scene(state):
    """Bounded data-only preview scene synchronized with the modeled terminal state."""
    terminal = ensure_cruise_terminal(state)
    config = terminal['config']
    objects = [_scene_object('cruise_terminal_building', 'Northstar Cruise Terminal', 'building',
                             [26350, 22500], [260, 16, 110],
                             'Terminal building serving all cruise calls; passenger halls listed below.')]
    for identifier, label, position, size, details in SCENE_AMENITIES:
        objects.append(_scene_object(identifier, label, 'building', position, size, details))
    objects.append(_scene_object('cruise_wharf', 'Cruise Wharf', 'dock',
                                 [SCENE_WHARF_X, 22500], [40, 6, 1100],
                                 'Main cruise wharf; berths lie along its east side.'))
    for identifier, label, position, size in SCENE_GANGWAYS:
        objects.append(_scene_object(identifier, label, 'dock', position, size,
                                     'Covered gangway between the terminal and the wharf.'))
    for identifier, label, position, size, details in SCENE_STAGING:
        objects.append(_scene_object(identifier, label, 'storage', position, size, details))
    if config['shuttle_capacity_people'] > 0:
        objects.append(_scene_object('cruise_shuttle_vehicle', 'Terminal Shuttle', 'vehicle',
                                     [26140, 22570], [12, 3, 2.5],
                                     'Shuttle staged at the bay (authored layout); capacity '
                                     f"{config['shuttle_capacity_people']} people every "
                                     f"{config['shuttle_headway_minutes']} minutes.", heading=0.0))
    objects.append(_scene_object('cruise_taxi_vehicle', 'Taxi (staged)', 'vehicle',
                                 [26140, 22430], [5, 1.8, 2],
                                 f"Taxi staged at the rank (authored layout); one group departs about every "
                                 f"{config['taxi_interval_seconds']} seconds uncongested.", heading=0.0))
    for visit in terminal['visits']:
        if visit['status'] in SCENE_STATUS_LABELS and visit['berth'] is not None:
            size = SCENE_SHIP_SIZES.get(visit['berth'], [55, 20, 260])
            position = [SCENE_WHARF_X + 20 + size[0] / 2 + 5, SCENE_BERTH_Z.get(visit['berth'], 22500)]
            ashore = sum(group['size'] for group in terminal['groups'] if group['visit'] == visit['id'])
            details = (f"{SCENE_STATUS_LABELS[visit['status']]} at berth {visit['berth']}; "
                       f"{ashore} passengers ashore, {visit['boarded_people']} aboard, "
                       f"{visit['missed_people']} missed, departs {_iso(visit['departure_ts'])[11:16]}.")
            objects.append(_scene_object(f"cruise_ship_berth_{visit['berth']}", visit['ship'], 'vehicle',
                                         position, size, details, heading=0.0))
        elif visit['status'] == 'holding':
            objects.append(_scene_object('cruise_ship_holding', visit['ship'], 'vehicle',
                                         SCENE_HOLDING_XY, [55, 20, 260],
                                         'Holding outside the harbour; awaiting a free berth.', heading=0.0))
    for destination_id, destination in DESTINATIONS.items():
        place = PLACE_INDEX[destination_id]
        objects.append(_scene_object(
            f"cruise_destination_{destination_id}", destination['name'], 'building',
            [place['x'], place['y']], [80, 10, 60],
            f"City destination at its shared venue anchor; capacity {destination['capacity']} visitors, "
            f"{_occupancy(terminal, destination_id)} on site now, "
            f"{terminal['totals']['visitors_by_destination'].get(destination_id, 0)} visitors this run."))
    roads = [{'id': identifier, 'points': [list(point) for point in points]}
             for identifier, points in SCENE_ROADS]
    for destination_id in DESTINATIONS:
        points, _ = _geometry(destination_id)
        stride = max(1, len(points) // 60)
        roads.append({'id': f"cruise_route_{destination_id}",
                      'points': [[point[0], point[1]] for point in points[::stride]]})
    return {'schema_version': SCENE_CONTRACT, 'units': 'm', 'synthetic': True,
            'geometry_note': SCENE_GEOMETRY_NOTE,
            'objects': objects[:SCENE_OBJECT_LIMIT],
            'roads': roads}
