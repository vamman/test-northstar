"""Actual authored weather decisions; no network I/O or source clock changes."""
from datetime import datetime
from zoneinfo import ZoneInfo
from .weather_policy import policy_for, attends
from .catalog import PLACE_INDEX

ESSENTIAL_ROLES = {'physical_security', 'housekeeping', 'reception', 'facilities'}


def policy(state, origin=None, destination=None):
    districts = [PLACE_INDEX.get(i, {}).get('district_id') for i in (origin, destination)]
    return policy_for(state.get('environment'), *districts)


def begin(state):
    previous = state.get('weather_response_outcomes', {})
    state['weather_response_outcomes'] = {'scope': 'latest_advance', 'started_at': state['t'],
        'people_before': state['generated_people'], 'road_before': state.get('road_traffic',{}).get('generated_vehicle_journeys',0),
        'requested_individual_trips': 0, 'suppressed_individual_trips': 0,
        'suppressed_road_vehicles': 0, 'requested_road_vehicles': 0,
        'suppressed_office_commutes': 0, 'essential_commutes_permitted': 0,
        'homebound_commutes_preserved': 0, 'decisions': [],
        'cumulative_suppressed_individual_trips': previous.get('cumulative_suppressed_individual_trips', 0),
        'cumulative_suppressed_road_vehicles': previous.get('cumulative_suppressed_road_vehicles', 0),
        'cumulative_suppressed_office_commutes': previous.get('cumulative_suppressed_office_commutes', 0)}


def allow(state, origin, destination, purpose, direction='outbound', weight=1, road=False):
    if 'weather_response_outcomes' not in state:
        begin(state)
    counts = state['weather_response_outcomes']
    key = 'road_vehicles' if road else 'individual_trips'
    counts['requested_'+key] += weight
    state['weather_attempt_sequence'] = state.get('weather_attempt_sequence', 0) + 1
    p = policy(state, origin, destination)
    factor = p['school_attendance_factor'] if purpose == 'school' else p['work_attendance_factor'] if purpose == 'work' else p['nonessential_travel_factor']
    # Aggregate work journeys include a declared essential-service share. Named
    # Office workers use their registered roles instead of this sample split.
    identity = f"sample:{state['weather_attempt_sequence']}:{origin}:{destination}"
    essential = purpose == 'work' and attends(identity+':essential', .15)
    if essential:
        factor = p['essential_staff_factor']
    if purpose == 'school' and p['school_closed']:
        factor = 0
    permitted = attends(identity, factor)
    if not permitted:
        counts['suppressed_'+key] += weight
        counts['cumulative_suppressed_'+key] += weight
    return permitted


def office_allowed(state, worker, workplace, event):
    from .commutes import HOME_PLACES
    counts = state['weather_response_outcomes']
    if event[4] == 'return':
        counts['homebound_commutes_preserved'] += 1
        return True
    day = datetime.fromtimestamp(event[2], ZoneInfo('America/Moncton')).date().isoformat()
    p = policy_for(state.get('environment'), HOME_PLACES[worker['home_place_id']]['district_id'], workplace['location']['district_id'])
    essential = worker['role_group'] in ESSENTIAL_ROLES
    fraction = p['essential_staff_factor'] if essential else p['work_attendance_factor']
    permit = attends(f"worker:{worker['worker_id']}:{day}", fraction)
    if permit and essential:
        counts['essential_commutes_permitted'] += 1
    if not permit:
        counts['suppressed_office_commutes'] += 1
        counts['cumulative_suppressed_office_commutes'] += 1
        if len(counts['decisions']) < 100:
            counts['decisions'].append({'worker_id': worker['worker_id'], 'shift_date': day,
                'decision': 'weather_remote' if worker['work_mode'] in ('hybrid','remote') and not essential else 'weather_absent',
                'policy_level': p['level']})
    return permit


def finish(state):
    counts = state['weather_response_outcomes']
    counts.update(generated_individual_trips=state['generated_people']-counts.pop('people_before'),
        generated_road_vehicles=state['road_traffic']['generated_vehicle_journeys']-counts.pop('road_before'),
        transit_vehicles_held_at_terminal=sum(v.get('weather_service_hold',False) for v in state['vehicles']),
        simulated_at=state['t'], active_individual_travelers=len(state['people']),
        active_road_vehicles=sum(v['vehicle_weight'] for v in state['road_traffic']['vehicles']),
        policy=policy(state))
    state['environment']['weather_response_outcomes'] = dict(counts)
