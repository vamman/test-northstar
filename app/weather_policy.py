"""Shared authored weather response contract; deterministic attendance decisions.

Vendor alongside environment_client.py. This contains no network or DB access.
"""
import copy
import hashlib
import math

VERSION = 'northstar.weather-response.v1'
FACTOR_KEYS = ('nonessential_travel_factor', 'work_attendance_factor', 'school_attendance_factor',
    'outpatient_attendance_factor', 'essential_staff_factor', 'emergency_demand_factor',
    'outdoor_work_factor', 'public_counter_factor', 'freight_factor',
    'public_transit_service_factor', 'travel_time_factor')
DISTRICTS = ('central','north','east','south','west','coastal','rural_north','rural_south')


def neutral():
    return {'pressure': 0.0, 'level': 'normal', 'school_closed': False,
            'remote_work_recommended': False, **{k: 1.0 for k in FACTOR_KEYS}}


def valid_policy(value):
    if not isinstance(value, dict) or not set(neutral()) <= set(value):
        return False
    if value['level'] not in ('normal','advisory','disruption','closure'):
        return False
    if any(type(value[k]) is not bool for k in ('school_closed','remote_work_recommended')):
        return False
    for k in ('pressure', *FACTOR_KEYS):
        v = value[k]
        low, high = (1, 4) if k in ('emergency_demand_factor','travel_time_factor') else (0, 1)
        if type(v) not in (int,float) or not math.isfinite(v) or not low <= v <= high:
            return False
    return value['essential_staff_factor'] >= .5 and value['public_transit_service_factor'] >= .1


def policy_for(observation, *district_ids):
    """Use the worst affected known origin/destination; otherwise regional policy.

    Missing/legacy/malformed response fields are neutral, never implicit closures.
    Current snapshot freshness is enforced by environment_client before application.
    """
    if not isinstance(observation, dict) or observation.get('status') not in ('connected','applied','fresh'):
        return neutral()
    response = observation.get('weather_response')
    if not isinstance(response, dict) or response.get('schema_version') != VERSION:
        return neutral()
    regional, districts = response.get('regional'), response.get('districts')
    if not valid_policy(regional) or not isinstance(districts, dict) or set(districts) != set(DISTRICTS) or not all(valid_policy(v) for v in districts.values()):
        return neutral()
    choices = [districts[d] for d in district_ids if isinstance(d,str) and d in districts]
    return copy.deepcopy(max(choices, key=lambda p:p['pressure']) if choices else regional)


def attends(identity, fraction):
    """Stable nested cohorts: stronger restrictions never add a previously excluded ID.

    Callers include a local date/shift/appointment in identity as appropriate. Existing
    records and already-started trips/care are never removed by this decision.
    """
    if fraction >= 1:
        return True
    if fraction <= 0:
        return False
    score = int.from_bytes(hashlib.sha256(str(identity).encode()).digest()[:8], 'big') / 2**64
    return score < fraction
