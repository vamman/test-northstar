"""The environment feed cannot silently keep an invalid or expired hazard active."""
from datetime import datetime, timedelta, timezone
import httpx
import pytest
from app import environment_client as client


@pytest.mark.parametrize('case', ['expired', 'wrong_peer', 'non_finite', 'connection_error'])
def test_environment_feed_failure_falls_back_and_next_read_recovers(monkeypatch, case):
    monkeypatch.setenv('ENVIRONMENT_API_URL', 'http://environment.test')
    client._cache.clear()
    peer = 'transit'
    body = client.neutral(peer)
    body.update(revision=123, valid_until=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
    body['multipliers']['productivity'] = .6
    bad = {**body, 'multipliers': dict(body['multipliers'])}
    if case == 'expired': bad['valid_until'] = '2020-01-01T00:00:00+00:00'
    if case == 'wrong_peer': bad['peer'] = 'elsewhere'
    if case == 'non_finite': bad['multipliers']['productivity'] = 'NaN'
    recovering = False
    def respond(request):
        assert request.url.path == f'/api/impacts/{peer}'
        if not recovering and case == 'connection_error':
            raise httpx.ConnectError('synthetic offline feed', request=request)
        return httpx.Response(200, json=body if recovering else bad)
    original = httpx.Client
    monkeypatch.setattr(client.httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    fallback = client.snapshot(peer)
    assert fallback['status'] == 'unavailable'
    assert all(value == 1. for value in fallback['multipliers'].values())
    recovering = True
    client._cache.clear()
    recovered = client.snapshot(peer)
    assert recovered['status'] == 'connected'
    assert recovered['revision'] == 123
    assert client.factor(recovered, 'productivity') == .6
    client._cache.clear()


def test_expired_cached_observation_is_not_reused(monkeypatch):
    monkeypatch.setenv('ENVIRONMENT_API_URL', 'http://environment.test')
    client._cache.clear()
    peer = 'transit'
    body = client.neutral(peer)
    body.update(revision=11, status='connected', valid_until='2020-01-01T00:00:00+00:00')
    import time
    client._cache[('http://environment.test', peer)] = (time.monotonic(), body)
    original = httpx.Client
    monkeypatch.setattr(client.httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kwargs))
    assert client.snapshot(peer)['status'] == 'unavailable'
    client._cache.clear()


@pytest.mark.parametrize('body', [None, [], {'valid_until': None}, {'valid_until': {}}])
def test_malformed_feed_is_neutral(monkeypatch, body):
    monkeypatch.setenv('ENVIRONMENT_API_URL', 'http://environment.test')
    client._cache.clear()
    original = httpx.Client
    monkeypatch.setattr(client.httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)), **kwargs))
    assert client.snapshot('transit')['status'] == 'unavailable'
    client._cache.clear()


def test_status_distinguishes_historical_evidence():
    from app.environment_effects import applied, report
    now = datetime.now(timezone.utc)
    source = client.neutral('transit', 'connected')
    source.update(revision=17, valid_until=(now + timedelta(seconds=30)).isoformat())
    stored = applied('transit', source, '2030-01-01T00:00:00+00:00', 'Fixture effect', ['productivity'])
    fresh = report(stored, 'transit', heartbeat=now.isoformat())
    assert fresh['applied'] and fresh['status'] == 'connected'
    assert report(stored, 'transit', paused=True, heartbeat=now.isoformat())['status'] == 'paused'
    assert report(stored, 'transit')['status'] == 'worker_stale'
    stored['valid_until'] = (now - timedelta(seconds=1)).isoformat()
    stale = report(stored, 'transit', heartbeat=now.isoformat())
    assert stale['status'] == 'stale' and not stale['applied']
    assert stale['revision'] == 17 and stale['last_applied']
    assert stored['status'] == 'connected'


def test_environment_mcp_tool_is_registered_read_only():
    import asyncio
    from app.mcp_server import mcp
    tool = next(t for t in asyncio.run(mcp.list_tools()) if t.name == 'get_environment_impact')
    assert tool.annotations.readOnlyHint and not tool.annotations.destructiveHint
