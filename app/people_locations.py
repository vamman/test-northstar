"""Read-only location publishing at the owning simulator, with bounded refreshes.

This module is vendored with its adapters. It never writes simulator state and
never turns home addresses, census estimates, or financial identities into GPS.
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .people_location_contract import CONTRACT, finite, point


def adapter(source):
    if source in {'office', 'factory', 'university', 'bank'}:
        from . import people_locations_work as module
    elif source in {'hospital', 'clinics', 'police', 'fire'}:
        from . import people_locations_health as module
    elif source in {'city', 'transit', '911', 'environment'}:
        from . import people_locations_civic as module
    else:
        raise ValueError('Unknown people-location source')
    return module


class PeopleLocationPublisher:
    def __init__(self, source, atlas, *, interval=8, collect_override=None):
        self.source = source
        self.atlas = atlas
        self.interval = max(3, interval)
        self.stale_after = max(40, self.interval * 3)
        self.collect_override = collect_override
        self.report = None
        self.error = None
        self.last_success = None
        self.lock = asyncio.Lock()

    async def refresh(self, fetch):
        async with self.lock:
            started = time.monotonic()
            observed = datetime.now(timezone.utc).isoformat()
            try:
                module = adapter(self.source)
                raw = await asyncio.wait_for(
                    self.collect_override() if self.collect_override else module.collect(self.source, fetch),
                    timeout=max(30, self.interval * 2))
                normalized = module.normalize(self.source, raw, self.atlas)
                records, seen = [], set()
                issues = list(normalized.get('issues', []))
                complete = normalized.get('complete') is True
                owned = 0
                for record in normalized.get('records', []):
                    identity = record.get('id')
                    weight = record.get('represented_people', 1)
                    if (record.get('source_id') != self.source or not identity or identity in seen
                            or not finite(weight) or weight <= 0):
                        complete = False
                        issues.append('Invalid or duplicate source record omitted.')
                        continue
                    seen.add(identity)
                    owned += weight
                    location = point(record.get('location'))
                    if location:
                        records.append({**record, 'location': location})
                total = normalized.get('total')
                if not finite(total) or total < 0:
                    total = None
                located = sum(r['represented_people'] for r in records)
                if total is not None and (total < owned or total < located):
                    complete = False
                    issues.append('Declared roster total is smaller than returned people.')
                    total = None
                if total is None and not records:
                    raise ValueError('No usable source roster')
                self.report = {
                    'contract': CONTRACT, 'source_id': self.source, 'synthetic': True,
                    'read_only': True, 'observed_at': observed,
                    'simulated_at': normalized.get('simulated_at'),
                    'paused': normalized.get('paused'), 'status': 'live',
                    'refresh_seconds': self.interval, 'stale_after': self.stale_after,
                    'collection_seconds': round(time.monotonic() - started, 3),
                    'records': records,
                    'coverage': {'total': total, 'located': located,
                        'unknown': max(0, total - located) if total is not None else None,
                        'records': len(records), 'owned_records': len(seen),
                        'complete': complete, 'supported': normalized.get('supported') is True},
                    'issues': list(dict.fromkeys(str(i)[:300] for i in issues)),
                    'provenance': 'Source-owned synthetic people. Facility/department assignments are coarse locations; absent positions remain unknown. Unknown records are omitted from this feed and retained in coverage counts.',
                }
                self.last_success = started
                self.error = None
            except Exception as exc:
                # No upstream URLs, credentials, or raw source data in errors.
                self.error = f'Location refresh failed ({type(exc).__name__}).'

    def snapshot(self):
        if self.report is None:
            return {'contract': CONTRACT, 'source_id': self.source, 'synthetic': True,
                    'read_only': True, 'observed_at': None, 'simulated_at': None,
                    'paused': None, 'status': 'offline', 'stale_after': self.stale_after,
                    'records': [], 'coverage': {'total': None, 'located': 0, 'unknown': None,
                    'records': 0, 'complete': False, 'supported': None},
                    'issues': [self.error or 'Waiting for the first source observation.']}
        result = copy.deepcopy(self.report)
        age = max(0, time.monotonic() - self.last_success)
        result['age_seconds'] = round(age, 2)
        if self.error or age > self.stale_after:
            result['status'] = 'stale'
        if self.error:
            result['issues'].append(self.error)
        return result

    async def run(self, app):
        semaphore = asyncio.Semaphore(4)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://source.internal', timeout=15) as client:
            async def fetch(path):
                if not path.startswith('/api/') or '://' in path:
                    raise ValueError('Only own API paths are allowed')
                async with semaphore:
                    response = await asyncio.wait_for(client.get(path), timeout=20)
                    response.raise_for_status()
                    if len(response.content) > 32_000_000:
                        raise ValueError('Source response exceeds 32 MB')
                    return response.json()
            while True:
                started = time.monotonic()
                await self.refresh(fetch)
                await asyncio.sleep(max(1, self.interval - (time.monotonic() - started)))


def attach_people_locations(app, source, *, atlas=None, interval=8, collect_override=None):
    if atlas is None:
        atlas = json.loads((Path(__file__).parent / 'people-location-atlas.json').read_text())
    publisher = PeopleLocationPublisher(source, atlas, interval=interval, collect_override=collect_override)
    app.state.people_locations = publisher

    async def endpoint():
        return publisher.snapshot()

    app.add_api_route('/api/people/locations', endpoint, methods=['GET'], tags=['Live locations'])
    route = app.router.routes.pop()
    app.router.routes.insert(0, route)  # Before generic resource/detail routes.
    prior = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with prior(application) as state:
            task = asyncio.create_task(publisher.run(application))
            try:
                yield state
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app.router.lifespan_context = lifespan
    return publisher
