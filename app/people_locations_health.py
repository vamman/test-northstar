"""Source-owned staff presence and anonymous inpatient census; no clinical charts."""
from __future__ import annotations

import asyncio

from .people_location_contract import facility, make_record, point, rows

SOURCES = {'clinics', 'hospital', 'police', 'fire'}
MAX_RECORDS = 10000
PAGE_SIZE = 200
AWAY = {'off_duty', 'off_shift', 'absent', 'sick', 'holiday', 'leave', 'weather_delay',
        'weather_delayed', 'remote', 'inactive', 'retired', 'unavailable'}
PRESENT = {'available', 'busy', 'working', 'on_duty', 'responding', 'assigned', 'onsite'}


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


async def _read(fetch, path, issues):
    try:
        value = await fetch(path)
        if not isinstance(value, dict):
            raise ValueError('Expected an object')
        return value
    except Exception as exc:
        issues.append(f'{path.split("?")[0]} could not be read ({type(exc).__name__}).')
        return {}


async def _staff_pages(fetch, issues):
    records, total, times, complete = [], None, set(), True
    for offset in range(0, MAX_RECORDS, PAGE_SIZE):
        page = await _read(fetch, f'/api/staff?limit={PAGE_SIZE}&offset={offset}', issues)
        chunk = rows(page)
        if offset == 0:
            total = page.get('total') if _count(page.get('total')) else None
        elif page.get('total') != total:
            complete = False
            issues.append('Staff roster size changed between pages.')
        if isinstance(page.get('as_of'), str):
            times.add(page['as_of'])
        records.extend(chunk[:PAGE_SIZE])
        if not page or len(chunk) > PAGE_SIZE:
            complete = False
        if total is None or len(chunk) < PAGE_SIZE or len(records) >= total:
            break
    if total is None or len(records) != total:
        complete = False
        issues.append('Full staff roster coverage could not be verified within the 10000-record limit.')
    if len(times) > 1:
        issues.append('Staff roster pages span source simulation timestamps.')
    return {'items': records, 'total': total, 'complete': complete,
            'as_of': max(times) if times else None}


async def collect(source, fetch):
    """Read only this simulator's public operational endpoints, bounded by owner."""
    issues = []
    if source not in SOURCES:
        return {'supported': False, 'issues': ['Source is outside the health adapter group.']}
    if source == 'hospital':
        staff, departments, clock = await asyncio.gather(
            _staff_pages(fetch, issues), _read(fetch, '/api/departments', issues),
            _read(fetch, '/api/clock', issues))
        return {'staff': staff, 'departments': departments, 'clock': clock, 'issues': issues}
    if source == 'clinics':
        operations, clinics, clock = await asyncio.gather(
            _read(fetch, '/api/operations', issues), _read(fetch, '/api/clinics', issues),
            _read(fetch, '/api/clock', issues))
        return {'operations': operations, 'clinics': clinics, 'clock': clock, 'issues': issues}
    return {'dashboard': await _read(fetch, '/api/dashboard', issues), 'issues': issues}


def _clock(data, dashboard, roster=None):
    simulation = dashboard.get('simulation', {})
    simulation = simulation if isinstance(simulation, dict) else {}
    clock = data.get('clock', {})
    clock = clock if isinstance(clock, dict) else {}
    members = rows(clock.get('members'))
    at = ((roster or {}).get('as_of') or dashboard.get('as_of') or simulation.get('simulated_at')
          or (members[0].get('simulated_at') if members else None))
    paused = simulation.get('paused')
    if not isinstance(paused, bool) and isinstance(simulation.get('running'), bool):
        paused = not simulation['running']
    authority = clock.get('authority', {})
    if not isinstance(paused, bool) and isinstance(authority, dict):
        paused = authority.get('paused')
    return at if isinstance(at, str) else None, paused if isinstance(paused, bool) else None


def _at_facility(atlas, identities, room=None, source_location=None):
    location = point(source_location) if source_location else None
    if location:
        location = {**location, 'basis': 'assigned_facility', 'precision': 'facility'}
    else:
        location = facility(atlas, identities)
    if location and isinstance(room, str) and room:
        location = {**location, 'room_id': room}
    return location


def normalize(source, data, atlas):
    issues = list(data.get('issues', []))
    result = {'records': [], 'total': None, 'complete': False, 'supported': source in SOURCES,
              'simulated_at': None, 'paused': None, 'issues': issues}
    if source not in SOURCES:
        return result
    dashboard = data.get('dashboard', {})
    dashboard = dashboard if isinstance(dashboard, dict) else {}
    complete, total, roster = True, None, []
    clinic_map, unit_map = {}, {}
    if source == 'hospital':
        staff = data.get('staff', {})
        staff = staff if isinstance(staff, dict) else {}
        roster, total, complete = rows(staff), staff.get('total'), staff.get('complete') is True
        result['simulated_at'], result['paused'] = _clock(data, dashboard, staff)
    elif source == 'clinics':
        dashboard = data.get('operations', {})
        dashboard = dashboard if isinstance(dashboard, dict) else {}
        staffing = dashboard.get('staffing', {})
        staffing = staffing if isinstance(staffing, dict) else {}
        roster, total = rows(staffing.get('clinicians')), staffing.get('total')
        complete = isinstance(staffing.get('clinicians'), list) and _count(total) and len(roster) == total
        clinic_map = {str(row['id']): row for row in rows(data.get('clinics')) if row.get('id')}
        result['simulated_at'], result['paused'] = _clock(data, dashboard)
        issues.append('Coverage includes the owned staff roster; clinics do not publish a verified patient attendance census.')
    else:
        key = 'officers' if source == 'police' else 'personnel'
        roster = rows(dashboard.get(key))
        total = len(dashboard[key]) if isinstance(dashboard.get(key), list) else None
        complete = total is not None and len(roster) == total
        unit_map = {str(row['id']): row for row in rows(dashboard.get('units' if source == 'police' else 'apparatus')) if row.get('id')}
        result['simulated_at'], result['paused'] = _clock(data, dashboard)
    if not _count(total):
        total, complete = None, False
    if len(roster) > MAX_RECORDS:
        complete = False
        issues.append('Staff roster exceeded the 10000-record limit.')
    seen = set()
    for person in roster[:MAX_RECORDS]:
        identity = person.get('id')
        if identity is None or not str(identity) or str(identity) in seen:
            complete = False
            issues.append('Excluded a staff record with a missing or repeated source identity.')
            continue
        seen.add(str(identity))
        status = str(person.get('status', 'unknown')).lower()
        location, reason, links = None, 'The source does not publish a current person location.', {}
        if source == 'hospital':
            links = {'department_id': person.get('department_id'), 'clinician_id': person.get('clinician_id')}
            away = 'away' in str(person.get('current_location', '')).lower()
            if status in PRESENT and not away:
                location = _at_facility(atlas, 'northstar_regional_hospital',
                                        person.get('current_location') or person.get('department_id'))
                reason = 'Source staff availability assigns this professional to the hospital; interior coordinates are not tracked.'
        elif source == 'clinics':
            clinic_id = str(person.get('clinic_id') or 'primary')
            links = {'clinic_id': clinic_id}
            if status in PRESENT:
                clinic = clinic_map.get(clinic_id, {})
                location = _at_facility(atlas, 'clinic_' + clinic_id, person.get('room'), clinic.get('location'))
                reason = 'Source on-shift clinical work assigns this professional to the clinic and named room.'
        elif source == 'police':
            links = {'unit_id': person.get('unit_id'), 'assigned_district_id': person.get('district_id')}
            unit = unit_map.get(str(person.get('unit_id')))
            if status in PRESENT and unit and str(identity) in {str(i) for i in unit.get('officer_ids', [])} and unit.get('status') not in AWAY:
                location = point(unit)
                if location:
                    location = {**location, 'basis': 'unit_last_modeled_position'}
                reason = 'Active unit membership; its last discrete modeled position, not a continuous person track.'
            elif status not in AWAY:
                reason = 'Only an assigned district is published.'
        else:
            assignment = str(person.get('assignment_id') or '')
            unit = unit_map.get(assignment)
            retained = (status == 'assigned' and unit and unit.get('status') not in AWAY
                        and str(identity) in {str(i) for i in unit.get('crew_ids', [])})
            links = {'station_id': person.get('station_id'), 'assignment_id': person.get('assignment_id'),
                     'shift_status': person.get('shift_status')}
            if retained:
                location = point(unit)
                if location:
                    location = {**location, 'basis': 'unit_last_modeled_position'}
                reason = 'Source retains this assigned response crew until release, including across shift changes; position is discrete.'
            elif status in {'available', 'on_duty'} and person.get('shift_status') == 'on_duty':
                station_id = str(person.get('station_id') or '')
                location = _at_facility(atlas, 'fire_' + station_id)
                reason = 'Source on-duty station assignment; no room position is tracked.'
            elif status == 'busy':
                reason = 'Work assignment has no published current position.'
        if status in AWAY:
            location, reason = None, 'Off-duty or absent; an assigned workplace or residence is not current attendance.'
        result['records'].append(make_record(source, identity, name=person.get('name', ''),
            role=person.get('role', 'professional'), status=status, location=location,
            links=links, location_reason=reason))
    if source == 'hospital':
        inpatient_total = 0
        departments = rows(data.get('departments'))
        if not departments:
            complete = False
            issues.append('The hospital inpatient census could not be verified.')
        for department in departments:
            if department.get('category') != 'inpatient':
                continue
            occupied = department.get('occupied')
            if not _count(occupied):
                complete = False
                issues.append('Excluded an invalid inpatient department census.')
                continue
            inpatient_total += occupied
            if occupied == 0:
                continue
            if not department.get('id') or len(result['records']) >= MAX_RECORDS:
                complete = False
                issues.append('An inpatient cohort could not be included within the identity/record bound.')
                continue
            location = _at_facility(atlas, 'northstar_regional_hospital', str(department['id']))
            if location:
                location = {**location, 'basis': 'facility_census'}
            result['records'].append(make_record(source, 'inpatient-census:' + str(department['id']),
                name='Inpatient department census', role='inpatient', status='in_care',
                entity_kind='cohort', location=location, weight=occupied,
                links={'department_id': department['id']},
                location_reason='Anonymous occupied inpatient beds (in-care plus boarding), excluding waiting lists and registries.'))
        if total is not None:
            total += inpatient_total
        issues.append('Coverage is owned staff plus anonymous occupied inpatient census; waiting lists, discharged registries, visitors and EMS crews are not tracked here.')
    result['total'] = total
    result['complete'] = complete and total is not None
    return result
