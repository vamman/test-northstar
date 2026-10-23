"""Source-owned people adapters for Transit, 911, City, and Environment.

Transit coordinates describe synthetic individual travelers, including riders.
Road-traffic representation weights and district populations are not person
rosters. Command operators have facility assignments, not tracked desk positions.
"""
from __future__ import annotations

import asyncio

from .people_location_contract import facility, make_record, point, rows


SUPPORTED_SOURCES = frozenset({'transit', '911', 'city', 'environment'})


async def _operators(fetch):
    """Read every reported operator page and expose any incomplete response."""
    items = []
    total = None
    issues = []
    limit = 200
    for page in range(100):
        response = await fetch(f'/api/operators?limit={limit}&offset={len(items)}')
        batch = rows(response)
        reported = response.get('total') if isinstance(response, dict) else None
        if isinstance(reported, int) and not isinstance(reported, bool) and reported >= 0:
            if total is not None and reported != total:
                issues.append('Operator roster total changed while reading pages.')
            total = reported
        items.extend(batch)
        if total is not None and len(items) >= total:
            break
        if len(batch) < limit:
            if total is not None and len(items) < total:
                issues.append('Operator endpoint returned fewer records than its reported total.')
            break
    else:
        issues.append('Operator pagination exceeded 100 pages; coverage is incomplete.')
    return {'items': items, 'total': total, 'issues': issues,
            'complete': total is not None and len(items) == total and not issues}


async def collect(source, fetch):
    """Read only this source's existing public APIs; never query a sibling."""
    if source == 'transit':
        # This API serializes all active people from one state snapshot. Paging
        # /api/people while the worker advances could omit or duplicate travelers.
        return {'dashboard': await fetch('/api/dashboard')}
    if source == '911':
        operators, health = await asyncio.gather(_operators(fetch), fetch('/api/health'))
        return {'operators': operators, 'health': health}
    if source in ('city', 'environment'):
        return {'health': await fetch('/api/health')}
    raise ValueError(f'Unsupported civic people source: {source}')


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _clock(data):
    """Preserve source simulation time and a genuine boolean pause value."""
    for value in (data.get('clock'), data.get('simulation'), data):
        value = _mapping(value)
        simulated_at = value.get('iso') or value.get('simulated_at') or value.get('time')
        paused = value.get('paused')
        if isinstance(simulated_at, str) and simulated_at:
            return simulated_at, paused if isinstance(paused, bool) else None
    return None, None


def _total(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _result(records, total, complete, supported, data, issues):
    simulated_at, paused = _clock(data)
    return {'records': records, 'total': total, 'complete': bool(complete),
            'supported': supported, 'simulated_at': simulated_at,
            'paused': paused, 'issues': issues}


def normalize(source, data, atlas):
    """Normalize owned entities; absent spatial evidence remains unknown."""
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f'Unsupported civic people source: {source}')
    data = _mapping(data)
    if source in ('city', 'environment'):
        reason = ('City models municipal service capacity and civic workflow records, '
                  'but owns no individual crew or visitor roster.' if source == 'city' else
                  'Environment models district conditions and aggregate population compartments, '
                  'but owns no individual people or staff roster.')
        return _result([], 0, True, False, _mapping(data.get('health')), [reason])

    if source == 'transit':
        dashboard = _mapping(data.get('dashboard'))
        people = dashboard.get('people')
        issues = []
        if not isinstance(people, list):
            return _result([], None, False, True, dashboard,
                           ['Transit dashboard did not contain its full individual traveler roster.'])
        reported = _total(_mapping(dashboard.get('stats')).get('active_people'))
        total = reported if reported is not None else len(people)
        if reported is not None and reported != len(people):
            issues.append('Transit active_people differs from the returned individual roster.')
        run_id = dashboard.get('run_id')
        if not isinstance(run_id, str) or not run_id:
            issues.append('Transit run_id is absent; source identities are not established across runs.')
            run_id = 'unknown-run'
        records, seen = [], set()
        for raw in people:
            person = _mapping(raw)
            ident = person.get('id')
            if not isinstance(ident, (str, int)) or isinstance(ident, bool) or not str(ident):
                issues.append('Transit returned an individual traveler without a source ID.')
                continue
            ident = str(ident)
            if ident in seen:
                issues.append(f'Transit returned duplicate individual traveler ID {ident}.')
                continue
            seen.add(ident)
            location = point(person, assume_metres=True)
            links = {}
            # This is an explicit Office-record relationship, not a census ID.
            if isinstance(person.get('office_worker_id'), str) and person['office_worker_id']:
                links['office_worker_id'] = person['office_worker_id']
            records.append(make_record(source, f'{run_id}:{ident}',
                name=person.get('name') or f'Traveler {ident}', role='traveler',
                status=person.get('status') or 'unknown', mode=person.get('mode'),
                location=location, links=links or None,
                location_reason=None if location else 'Transit did not report valid individual x/y coordinates.'))
        return _result(records, total, not issues and len(records) == total, True, dashboard, issues)

    operators = _mapping(data.get('operators'))
    health = _mapping(data.get('health'))
    items = rows(operators)
    total = _total(operators.get('total'))
    issues = list(operators.get('issues') or [])
    if total is None:
        issues.append('911 operator endpoint did not report the total roster size.')
    if operators.get('complete') is False and not issues:
        issues.append('911 operator collection reported incomplete coverage.')
    assigned_facility = facility(atlas, 'northstar_command')
    records, seen = [], set()
    for raw in items:
        operator = _mapping(raw)
        ident = operator.get('id')
        if not isinstance(ident, (str, int)) or isinstance(ident, bool) or not str(ident):
            issues.append('911 returned an operator without a source ID.')
            continue
        ident = str(ident)
        if ident in seen:
            issues.append(f'911 returned duplicate operator ID {ident}.')
            continue
        seen.add(ident)
        status = operator.get('status') or 'unknown'
        location = dict(assigned_facility) if assigned_facility and status in ('available', 'busy') else None
        if location:
            reason = 'Command roster assignment; this simulator does not model individual desks or commutes.'
        else:
            reason = ('Attendance-restricted or inactive operator has no reported current location.'
                      if status not in ('available', 'busy') else 'Command facility anchor is unavailable.')
        records.append(make_record(source, ident, name=operator.get('name') or ident,
            role=operator.get('role') or 'operator', status=status, location=location,
            location_reason=reason))
    return _result(records, total, total is not None and len(records) == total and not issues,
                   True, health, issues)
