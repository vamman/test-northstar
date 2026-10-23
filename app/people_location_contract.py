"""Sanitized, source-owned synthetic person-location reports. No census inference."""
from __future__ import annotations

import math

CONTRACT = 'northstar.people-locations.v1'
CRS = 'northstar-local-m-v1'


def rows(value):
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        return rows(value.get('items', value.get('results', [])))
    return []


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def point(record, assume_metres=False):
    if not isinstance(record, dict):
        return None
    raw = record.get('location') if isinstance(record.get('location'), dict) else record
    crs = raw.get('crs', raw.get('coordinate_system'))
    if crs == CRS:
        x, y = raw.get('x'), raw.get('y')
    elif finite(record.get('world_x')) and finite(record.get('world_y')):
        x, y = record['world_x'], record['world_y']
    elif crs == 'northstar-local-v1':
        x, y = raw.get('x'), raw.get('y')
        if not finite(x) or not finite(y):
            return None
        x, y = x * 1000, y * 1000
    elif assume_metres:
        x, y = raw.get('x'), raw.get('y')
    else:
        return None
    if not finite(x) or not finite(y) or abs(x) > 250000 or abs(y) > 250000:
        return None
    result = {'crs': CRS, 'units': 'm', 'x': round(x, 3), 'y': round(y, 3),
              'basis': raw.get('basis', 'simulated_point')}
    for key in ('facility_id', 'place_id', 'district_id', 'precision', 'room_id', 'floor', 'area_id'):
        value = raw.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            result[key] = str(value)[:120] if isinstance(value, str) else value
    return result


def facility(atlas, id_or_list):
    ids = [id_or_list] if isinstance(id_or_list, str) else list(id_or_list or [])
    if not isinstance(atlas, dict):
        return None
    candidates = rows(atlas.get('places')) + rows(atlas.get('facilities'))
    for identity in ids:
        for row in candidates:
            if str(row.get('id')) != str(identity):
                continue
            loc = point(row, assume_metres=atlas.get('crs') == CRS or 'roads' in atlas)
            if loc:
                return {**loc, 'basis': 'assigned_facility', 'facility_id': str(identity),
                        'precision': 'facility'}
    return None


def make_record(source, entity_id, *, name='', role='person', status='unknown', mode=None,
                location=None, weight=1, entity_kind='person', links=None, location_reason=None):
    if entity_id is None or not str(entity_id):
        raise ValueError('A stable source entity ID is required')
    identity = str(entity_id)[:240]
    loc = point(location) if location else None
    if not finite(weight) or weight <= 0:
        raise ValueError('Represented person weight must be positive and finite')
    record = {'id': f'{source}:{identity}', 'source_id': str(source), 'source_entity_id': identity,
              'name': str(name or '')[:120], 'role': str(role or 'person')[:80],
              'status': str(status or 'unknown')[:80], 'entity_kind': entity_kind,
              'represented_people': weight, 'location': loc,
              'location_state': 'located' if loc else 'unknown'}
    if mode is not None:
        record['mode'] = str(mode)[:40]
    if location_reason:
        record['location_reason'] = str(location_reason)[:240]
    if isinstance(links, dict):
        record['links'] = {str(k)[:60]: str(v)[:240] for k, v in links.items()
                           if v is not None and isinstance(v, (str, int)) and not isinstance(v, bool)}
    return record
