"""Source-owned people reports for workplaces, the university and the bank.

Locations are facility/campus anchors backed by current source attendance, not
precise person tracking. Unknown workers remain unknown. University deliberately
does not download its student registry merely to repeat unknown locations: its
active registry total accounts for students without current onsite attendance.
The bank's financial identity count is explicitly not a population/location feed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

try:
    from .people_location_contract import facility, make_record, point, rows
except ImportError:  # Also usable as the vendored app module / standalone tests.
    from people_location_contract import facility, make_record, point, rows


PAGE_SIZE = 200
MAX_ROSTER = 10_000  # Per facility, not a global cap on the 19,620 office workers.
MAX_FACILITIES = 64
CONCURRENCY = 4


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _identity(row):
    value = row.get('id') if isinstance(row, dict) else None
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value) else None


async def _read(fetch, path, issues):
    try:
        value = await fetch(path)
    except Exception:
        issues.append(f'Could not read {path.split("?")[0]}.')
        return None
    if not isinstance(value, dict):
        issues.append(f'Malformed response from {path.split("?")[0]}.')
        return None
    return value


async def _pages(fetch, path, params=None, *, cap=MAX_ROSTER):
    """Read bounded pages, retaining useful rows while reporting partial coverage."""
    result = {'items': [], 'total': None, 'complete': True, 'issues': [], 'simulation_times': []}
    offset, seen = 0, set()
    while offset < cap:
        limit = min(PAGE_SIZE, cap - offset)
        query = urlencode({**(params or {}), 'limit': limit, 'offset': offset})
        page = await _read(fetch, f'{path}?{query}', result['issues'])
        if page is None or not isinstance(page.get('items'), list):
            result['complete'] = False
            if page is not None:
                result['issues'].append(f'Malformed page items from {path}.')
            break
        total = _count(page.get('total'))
        if result['total'] is None:
            result['total'] = total
        elif total != result['total']:
            result['issues'].append(f'Roster total changed while paging {path}.')
            result['complete'] = False
        if total is None:
            result['issues'].append(f'No valid roster total from {path}.')
            result['complete'] = False
        timestamp = page.get('simulation_time')
        if isinstance(timestamp, str) and timestamp not in result['simulation_times']:
            result['simulation_times'].append(timestamp)
        batch = page['items']
        if len(batch) > limit:
            result['issues'].append(f'Oversized page from {path}.')
            result['complete'] = False
            batch = batch[:limit]
        added = 0
        for row in batch:
            ident = _identity(row)
            if ident is None or ident in seen:
                result['complete'] = False
                result['issues'].append(f'Missing or duplicate identity in {path}.')
                continue
            seen.add(ident)
            result['items'].append(row)
            added += 1
        offset += len(batch)
        if total is not None and offset >= total:
            break
        if not batch or not added or len(batch) < limit:
            if total is not None and offset < total:
                result['complete'] = False
                result['issues'].append(f'Roster ended before its declared total in {path}.')
            break
    if result['total'] is not None and len(result['items']) < result['total']:
        result['complete'] = False
        if offset >= cap:
            result['issues'].append(f'Roster capped at {cap} records in {path}.')
    if result['total'] is not None and len(result['items']) > result['total']:
        result['complete'] = False
        result['issues'].append(f'Roster exceeded its declared total in {path}.')
    result['issues'] = list(dict.fromkeys(result['issues']))
    return result


async def collect(source, fetch):
    """Read only this source's local GET APIs; never request a peer or mutate it."""
    issues = []
    if source in ('office', 'factory'):
        inventory_path = '/api/buildings' if source == 'office' else '/api/plants'
        inventory, clock = await asyncio.gather(
            _read(fetch, inventory_path, issues), _read(fetch, '/api/clock', issues))
        facilities = rows(inventory)
        inventory_total = _count((inventory or {}).get('total'))
        complete = inventory_total is not None and inventory_total == len(facilities)
        if not complete:
            issues.append('Facility inventory is incomplete or has no valid total.')
        if any(_identity(b) is None for b in facilities) or len({_identity(b) for b in facilities}) != len(facilities):
            complete = False
            issues.append('Facility inventory has missing or duplicate identities.')
            unique = {}
            for building in facilities:
                if _identity(building):
                    unique.setdefault(_identity(building), building)
            facilities = list(unique.values())
        if len(facilities) > MAX_FACILITIES:
            complete = False
            issues.append(f'Facility inventory capped at {MAX_FACILITIES}.')
        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def roster(building):
            async with semaphore:
                ident = _identity(building)
                if ident is None:
                    return {'facility': building, 'workers': None, 'visitors': None}
                param = {'building_id' if source == 'office' else 'plant_id': ident}
                workers = await _pages(fetch, '/api/workers', param)
                visitors = await _pages(fetch, '/api/visitors', param) if source == 'office' else None
                return {'facility': building, 'workers': workers, 'visitors': visitors}

        rosters = await asyncio.gather(*(roster(b) for b in facilities[:MAX_FACILITIES]))
        return {'rosters': rosters, 'clock': clock, 'complete': complete, 'issues': issues}
    if source == 'university':
        async def active_count():
            return await _read(fetch, '/api/entities/students?status=active&limit=1&offset=0', issues)

        clock, campus, staff, students, teaching, examining = await asyncio.gather(
            _read(fetch, '/api/clock', issues), _read(fetch, '/api/campus', issues),
            _pages(fetch, '/api/entities/staff', {'status': 'active'}), active_count(),
            _pages(fetch, '/api/entities/sections', {'status': 'in_progress'}),
            _pages(fetch, '/api/entities/sections', {'status': 'examining'}))
        return {'clock': clock, 'campus': campus, 'staff': staff, 'students': students,
                'sections': [teaching, examining], 'issues': issues}
    if source == 'bank':
        clock, accounts = await asyncio.gather(
            _read(fetch, '/api/clock', issues),
            _read(fetch, '/api/accounts?kind=person&limit=1&offset=0', issues))
        return {'clock': clock, 'accounts': accounts, 'issues': issues}
    return {'issues': ['Unsupported work-source adapter.'], 'complete': False}


def _clock(data):
    clock = data.get('clock') if isinstance(data.get('clock'), dict) else {}
    authority = clock.get('authority') if isinstance(clock.get('authority'), dict) else {}
    # The member clock is the source's applied state; authority.target_at alone
    # does not establish that the source has committed that minute.
    timestamps = [m.get('simulated_at') for m in rows(clock.get('members')) if isinstance(m.get('simulated_at'), str)]
    parsed = []
    for stamp in timestamps:
        try:
            value = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            if value.tzinfo is not None:
                parsed.append((value.timestamp(), stamp))
        except (ValueError, TypeError, OverflowError):
            pass
    simulated_at = min(parsed)[1] if parsed else None
    paused = authority.get('paused')
    paused = paused if isinstance(paused, bool) else None
    return simulated_at, paused, len({t[0] for t in parsed}) > 1


def _result(data, *, supported=True):
    simulated_at, paused, differing = _clock(data)
    result = {'records': [], 'total': None, 'complete': True, 'supported': supported,
              'simulated_at': simulated_at, 'paused': paused,
              'issues': list(data.get('issues', [])) if isinstance(data.get('issues'), list) else []}
    if differing:
        result['issues'].append('Facility source clocks differ; this is not one atomic city snapshot.')
    return result


def _append_page_issues(result, page, label):
    if not isinstance(page, dict):
        result['complete'] = False
        result['issues'].append(f'Missing {label} roster.')
        return
    if page.get('complete') is not True:
        result['complete'] = False
    if isinstance(page.get('issues'), list):
        result['issues'].extend(str(i) for i in page['issues'])


def _onsite_anchor(native, atlas, *, facility_id=None, precision='facility', assume_metres=True):
    location = point(native, assume_metres=assume_metres)
    if location is None and facility_id:
        location = facility(atlas, facility_id)
    if location is None:
        return None
    location = {**location, 'basis': 'assigned_facility', 'precision': precision}
    if facility_id:
        location['facility_id'] = facility_id
    return location


def _workplaces(source, data, atlas):
    result = _result(data)
    result['complete'] = data.get('complete') is True
    totals, seen = [], set()
    for roster in rows(data.get('rosters')):
        building = roster.get('facility') if isinstance(roster.get('facility'), dict) else {}
        building_id = _identity(building)
        for resource in ('workers', 'visitors') if source == 'office' else ('workers',):
            page = roster.get(resource)
            _append_page_issues(result, page, resource)
            total = _count((page or {}).get('total')) if isinstance(page, dict) else None
            totals.append(total)
            for person in rows(page):
                ident = _identity(person)
                if ident is None or ident in seen:
                    result['complete'] = False
                    result['issues'].append('Missing or repeated source person identity.')
                    continue
                seen.add(ident)
                status = str(person.get('status') or 'unknown')
                onsite = status == ('checked_in' if resource == 'visitors' else 'onsite')
                location = None
                assigned_id = person.get('building_id')
                if onsite and assigned_id not in (None, building_id):
                    result['complete'] = False
                    result['issues'].append('Person facility differs from its source roster; location withheld.')
                elif onsite:
                    location = _onsite_anchor(building, atlas, facility_id=building_id)
                    if location is not None:
                        floor = person.get('floor') if resource == 'visitors' else person.get('location_floor')
                        if isinstance(floor, (int, str)) and not isinstance(floor, bool):
                            location['floor'] = floor
                        area = person.get('location_area_id')
                        if isinstance(area, str) and area:
                            location['area_id'] = area
                reason = ('Current source attendance; coordinates identify the facility, not a tracked person.'
                          if location else 'No current source-supported physical location.')
                result['records'].append(make_record(
                    source, ident, name=person.get('name'), role=person.get('role') or ('visitor' if resource == 'visitors' else 'worker'),
                    status=status, location=location, location_reason=reason))
    result['total'] = sum(totals) if data.get('complete') is True and totals and all(t is not None for t in totals) else None
    # An explicitly empty, valid source inventory is a complete zero roster.
    if not data.get('rosters') and data.get('complete') is True:
        result['total'] = 0
    return result


def _attendance_date_current(attendance, simulated_at):
    date = attendance.get('date')
    if isinstance(date, str) and isinstance(simulated_at, str):
        try:
            current = datetime.fromisoformat(simulated_at.replace('Z', '+00:00')).astimezone(ZoneInfo('America/Moncton')).date().isoformat()
            if date != current:
                return False
        except (ValueError, TypeError, OverflowError):
            return False
    return True


def _current_attendance(section, simulated_at):
    attendance = section.get('weather_attendance')
    if not isinstance(attendance, dict) or not isinstance(attendance.get('onsite_student_ids'), list):
        return None
    return attendance if _attendance_date_current(attendance, simulated_at) else None


def _university(data, atlas):
    result = _result(data)
    staff_page = data.get('staff')
    student_page = data.get('students') if isinstance(data.get('students'), dict) else {}
    _append_page_issues(result, staff_page, 'staff')
    staff_total = _count((staff_page or {}).get('total')) if isinstance(staff_page, dict) else None
    student_total = _count(student_page.get('total'))
    result['total'] = staff_total + student_total if staff_total is not None and student_total is not None else None
    if result['total'] is None:
        result['complete'] = False
        result['issues'].append('Active student/staff registry totals are unavailable.')
    campus = data.get('campus') if isinstance(data.get('campus'), dict) else {}
    campus_location = _onsite_anchor(campus.get('location') or campus.get('world_anchor'), atlas,
                                    facility_id='university', precision='campus')
    buildings = {_identity(b): b for b in rows(campus.get('buildings')) if _identity(b)}
    student_locations, teaching_locations, conflicting = {}, {}, set()
    section_pages = data.get('sections') if isinstance(data.get('sections'), list) else []
    if len(section_pages) != 2:
        result['complete'] = False
        result['issues'].append('Current teaching and examining coverage is incomplete.')
    for page in section_pages:
        _append_page_issues(result, page, 'current sections')
        stamps = page.get('simulation_times', []) if isinstance(page, dict) else []
        stamp = stamps[-1] if stamps else campus.get('simulation_time') or result['simulated_at']
        for section in rows(page):
            if section.get('status') not in ('in_progress', 'examining'):
                result['complete'] = False
                result['issues'].append('Non-current section ignored in current attendance response.')
                continue
            attendance = _current_attendance(section, stamp)
            if attendance is None:
                result['complete'] = False
                result['issues'].append('Current section attendance is missing or belongs to another date.')
                continue
            building_id = section.get('building_id')
            # University x/y are a campus-local overlay. Only world_x/world_y
            # or an explicitly declared world CRS may locate a campus building.
            location = _onsite_anchor(buildings.get(building_id), {}, facility_id=building_id,
                                      assume_metres=False) if isinstance(building_id, str) else None
            location = location or campus_location
            if location is not None and section.get('room') is not None:
                location = {**location, 'room_id': str(section['room'])}
            for ident in attendance['onsite_student_ids']:
                if not isinstance(ident, str) or not ident:
                    result['complete'] = False
                    result['issues'].append('Malformed onsite student identity.')
                    continue
                if ident in student_locations and student_locations[ident] != location:
                    conflicting.add(ident)
                else:
                    student_locations[ident] = location
            instructor = section.get('instructor_id')
            if isinstance(instructor, str) and instructor:
                if instructor in teaching_locations and teaching_locations[instructor] != location:
                    conflicting.add(instructor)
                else:
                    teaching_locations[instructor] = location
    if conflicting:
        result['complete'] = False
        result['issues'].append('Conflicting current section locations withheld for affected identities.')
    for person in rows(staff_page):
        ident = _identity(person)
        if ident is None:
            result['complete'] = False
            continue
        attendance = person.get('weather_attendance') if isinstance(person.get('weather_attendance'), dict) else {}
        status = str(attendance.get('status') or 'unknown')
        # Employment status "active" and assigned department are not attendance.
        stamps = staff_page.get('simulation_times', []) if isinstance(staff_page, dict) else []
        stamp = stamps[-1] if stamps else campus.get('simulation_time') or result['simulated_at']
        current = _attendance_date_current(attendance, stamp)
        onsite = person.get('status') == 'active' and status == 'onsite' and current
        if not current:
            result['complete'] = False
            result['issues'].append('Staff attendance belongs to another source date; location withheld.')
        location = teaching_locations.get(ident, campus_location) if onsite and ident not in conflicting else None
        result['records'].append(make_record(
            'university', ident, name=person.get('name'), role=person.get('role') or 'staff',
            status=status, location=location,
            location_reason='Current source campus attendance; anchor precision only.' if location else 'No current source-supported physical location.'))
    for ident, location in sorted(student_locations.items()):
        result['records'].append(make_record(
            'university', ident, role='student', status='onsite',
            location=None if ident in conflicting else location,
            location_reason='Named onsite attendance in a current source section; building anchor precision.'))
    result['issues'].append('Student registry total covers active students; students without named current onsite attendance have unknown locations and are omitted from records.')
    result['issues'].append('Residence assignments, department assignments and scheduled activity participants are not current physical presence.')
    if result['total'] is not None and len(result['records']) > result['total']:
        result['complete'] = False
        result['issues'].append('Attendance records exceed the declared active registry count.')
    return result


def normalize(source, data, atlas):
    """Normalize only evidence owned by this source, without cross-source merging."""
    data = data if isinstance(data, dict) else {}
    if source in ('office', 'factory'):
        result = _workplaces(source, data, atlas)
    elif source == 'university':
        result = _university(data, atlas)
    elif source == 'bank':
        result = _result(data, supported=False)
        accounts = data.get('accounts') if isinstance(data.get('accounts'), dict) else {}
        result['total'] = _count(accounts.get('total'))
        result['complete'] = result['total'] is not None
        result['issues'].append('Bank total is a financial identity registry, not city population or live presence; this source owns no physical people locations.')
    else:
        result = _result(data, supported=False)
        result['complete'] = False
        result['issues'].append('Unsupported work-source adapter.')
    result['issues'] = list(dict.fromkeys(result['issues']))
    return result
