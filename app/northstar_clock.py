"""Northstar clock client. Vendored unchanged; runtime target is mounted read-only.

Only committed local state acknowledges a target. Reading a target never acknowledges
it, so rollback/restart cannot skip a minute. Enabled clients fail closed.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

CONTRACT = 'northstar.clock.v1'
TIME_ACTIONS = {'pause', 'resume', 'speed', 'step', 'advance', 'jump', 'reset', 'next_day', 'next_week', 'start_workday'}


def enabled():
    return bool(os.getenv('NORTHSTAR_CLOCK_FILE'))


def timestamp(value):
    if isinstance(value, (float, int)):
        return float(value)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def iso(value):
    return datetime.fromtimestamp(timestamp(value), timezone.utc).isoformat()


def target():
    p = json.loads(Path(os.environ['NORTHSTAR_CLOCK_FILE']).read_text())
    if p.get('contract') != CONTRACT or not p.get('epoch') or p.get('step_seconds') != 60:
        raise ValueError('Invalid Northstar clock contract')
    age = datetime.now(timezone.utc).timestamp() - timestamp(p['heartbeat_at'])
    if not -5 <= age <= 120:
        raise ValueError('Northstar clock authority heartbeat expired; time is held')
    return p


def seconds(current, metadata):
    """Call under the application's state lock; never fall back to a local clock."""
    p = target()
    binding = metadata.get('shared_clock', {})
    if binding.get('epoch') != p['epoch']:
        raise ValueError('Northstar clock epoch does not match the migrated state')
    delta = timestamp(p['target_at']) - timestamp(current)
    if delta < -0.001 or delta > 60.001:
        raise ValueError('Local clock is outside the coordinated one-minute window')
    return max(0, round(delta, 6))


def local_snapshot(peer):
    """Small DB read; no simulator initialization or observation network I/O."""
    from sqlalchemy import text
    from app import db
    tables = {'office': ('office_simulation_state', 'clock', 'data', 'building_id'),
              'university': ('simulation_state', 'clock', 'data', 'id'),
              'transit': ('simulation', None, 'payload', 'id'),
              'city': ('city_state', None, 'payload', 'id'),
              'bank': ('bank_state', None, 'payload', 'id'),
              'environment': ('environment_state', None, 'payload', 'id')}
    table, column, meta, key = tables.get(peer, ('simulation_state', 'simulated_at', 'settings', 'id'))
    with db.engine.connect() as conn:
        if conn.dialect.name == 'postgresql':
            clock_expr = column or (f"{meta}->>'t'" if peer == 'transit' else f"{meta}->>'simulated_at'")
            heartbeat_expr = ('worker_at' if peer == 'transit' else f"{meta}->>'worker_heartbeat_at'" if peer == 'office'
                              else f"{meta}->>'heartbeat_at'" if peer == 'university'
                              else f"{meta}->>'worker_last_seen_at'" if peer in ('city','bank','environment') else 'last_tick_at')
            rows = conn.execute(text(f"SELECT {key} AS member, {clock_expr} AS at, {meta}->'shared_clock' AS binding, {heartbeat_expr} AS heartbeat FROM {table} ORDER BY {key}")).mappings().all()
            return [{'id': str(r['member']), 'simulated_at': iso(float(r['at']) if peer == 'transit' else r['at']),
                     'epoch': (r['binding'] or {}).get('epoch'), 'cutover': r['binding'] or {},
                     'worker_at': iso(r['heartbeat']) if r['heartbeat'] else None} for r in rows]
        rows = conn.execute(text(f'SELECT * FROM {table} ORDER BY {key}')).mappings().all()
    members = []
    for r in rows:
        data = r[meta]
        if isinstance(data, str):
            data = json.loads(data)
        at = r[column] if column else data['t'] if peer == 'transit' else data['simulated_at']
        binding = data.get('shared_clock', {})
        heartbeat = (r.get('last_tick_at') or r.get('worker_at') or
                     data.get('worker_heartbeat_at') or data.get('worker_last_seen_at') or data.get('heartbeat_at'))
        # University writes its heartbeat into data; all other variants above are explicit.
        members.append({'id': str(r[key]), 'simulated_at': iso(at),
                        'epoch': binding.get('epoch'), 'cutover': binding,
                        'worker_at': iso(heartbeat) if heartbeat else None})
    return members


def attach(app, peer):
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.get('/api/clock', tags=['Shared clock'])
    def clock_status():
        if not enabled():
            return {'mode': 'independent', 'peer': peer, 'contract': CONTRACT}
        try:
            p = target()
            members = local_snapshot(peer)
            lag = max((timestamp(p['target_at']) - timestamp(m['simulated_at']) for m in members), default=None)
            return {'mode': 'shared', 'peer': peer, 'contract': CONTRACT, 'authority': p,
                    'members': members, 'lag_seconds': lag,
                    'caught_up': bool(members) and lag == 0 and all(m['epoch'] == p['epoch'] for m in members)}
        except Exception as exc:
            return JSONResponse({'mode': 'shared', 'peer': peer, 'status': 'held',
                                 'error': str(exc)}, status_code=503)

    # Some peers define /api/{resource}; the fixed clock route must precede it.
    route = next(r for r in app.router.routes if getattr(r, 'endpoint', None) is clock_status)
    app.router.routes.remove(route)
    app.router.routes.insert(0, route)

    @app.middleware('http')
    async def protect_clock(request: Request, call_next):
        path = request.url.path.rstrip('/')
        if enabled() and request.method in ('POST', 'PATCH', 'PUT', 'DELETE'):
            direct = path.endswith(('/advance', '/step', '/jump', '/reset'))
            control = path.endswith(('/control', '/controls'))
            blocked = direct
            if control:
                try:
                    body = await request.json()
                    blocked = (isinstance(body, dict) and
                               (body.get('action') in TIME_ACTIONS or any(k in body for k in ('running', 'paused', 'speed'))))
                except (ValueError, TypeError):
                    pass  # Existing endpoint validation handles invalid input.
            if blocked:
                return JSONResponse({'detail': 'Time is managed by the shared Northstar clock. Use the Northstar clock controls.',
                                     'clock_url': '/api/clock'}, status_code=409)
        return await call_next(request)
