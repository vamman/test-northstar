"""Northstar environment v1 pull contract; fail neutral when unavailable or stale.

Vendored into each simulator. Reads only the configured environmental service.
Workers should persist snapshot() beside operational state when applying factors.
"""
import copy
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

_lock = threading.Lock()
_cache = {}
KEYS = ('demand', 'travel_time', 'capacity', 'productivity', 'absence', 'utility_demand')


def neutral(peer, status='unavailable', error=None):
    return {'contract_version': 'northstar.environment.v1', 'synthetic': True,
            'peer': peer, 'status': status, 'revision': None,
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'environment_simulated_at': None, 'multipliers': {k: 1.0 for k in KEYS},
            'drivers': [], 'error': error}


def snapshot(peer):
    url = os.getenv('ENVIRONMENT_API_URL', 'http://127.0.0.1:8865').rstrip('/')
    if not url:
        return neutral(peer, 'disabled')
    key = (url, peer)
    with _lock:
        previous = _cache.get(key)
        if previous and time.monotonic() - previous[0] < 10:
            cached = previous[1]
            if cached.get('status') != 'connected' or datetime.fromisoformat(cached['valid_until'].replace('Z', '+00:00')) > datetime.now(timezone.utc):
                return copy.deepcopy(cached)
        result = neutral(peer)
        try:
            with httpx.Client(timeout=1.5, follow_redirects=False, trust_env=False) as client:
                response = client.get(f'{url}/api/impacts/{peer}')
                response.raise_for_status()
                body = response.json()
            now = datetime.now(timezone.utc)
            if not isinstance(body, dict) or not isinstance(body.get('valid_until'), str):
                raise ValueError('Invalid expiry timestamp')
            expires = datetime.fromisoformat(body['valid_until'].replace('Z', '+00:00'))
            if body.get('synthetic') is not True or body.get('contract_version') != 'northstar.environment.v1' or body.get('peer') != peer:
                raise ValueError('Unsupported environment contract')
            if expires <= now or expires > now + timedelta(minutes=5):
                raise ValueError('Expired environment observation')
            factors = body['multipliers']
            if not isinstance(factors, dict) or set(factors) != set(KEYS) or any(type(v) not in (int, float) or not math.isfinite(v) or not 0.1 <= v <= 5 for v in factors.values()):
                raise ValueError('Invalid environment factors')
            result = {**body, 'status': 'connected', 'observed_at': now.isoformat(), 'error': None}
        except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError):
            result['error'] = 'Environment feed unavailable, invalid, or stale; neutral factors applied.'
        _cache[key] = (time.monotonic(), result)
        return copy.deepcopy(result)


def factor(observation, name):
    return float(observation['multipliers'].get(name, 1.0))
