"""Offline detailed-map reads preserve the operational Transit catalog and state."""
import copy
import hashlib
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import basemap, catalog
from test_api import client
from test_workplace_api import submit


def test_standalone_basemap_endpoints_need_no_database_or_partner(monkeypatch):
    from app import db
    def forbidden(*args, **kwargs):
        raise AssertionError('Basemap reads must not access operational databases')
    monkeypatch.setattr(db, 'Session', forbidden)
    app = FastAPI(); basemap.attach_basemap_routes(app)
    with TestClient(app) as browser:
        result = browser.get('/api/basemap')
        assert result.status_code == 200
        assert result.content == basemap.BASEMAP_BYTES
        assert hashlib.sha256(result.content).hexdigest() == result.headers['x-content-sha256']
        assert result.headers['etag'] == '"' + basemap.BASEMAP_SHA256 + '"'
        meta = browser.get('/api/basemap/metadata').json()
        assert meta['counts']['roads'] == 1147
        assert meta['routing']['shared_analysis_graph']['road_count'] == 93
        first = browser.get('/api/basemap/features', params={'layer': 'places', 'limit': 2}).json()
        second = browser.get('/api/basemap/features', params={'layer': 'places', 'limit': 2, 'offset': 2}).json()
        assert first['total'] == 78 == second['total']
        assert not {item['id'] for item in first['items']} & {item['id'] for item in second['items']}
        rural = browser.get('/api/basemap/features', params={'layer': 'places', 'district_id': 'rural_north'}).json()
        assert rural['items'] and all(item['district_id'] == 'rural_north' for item in rural['items'])
        assert browser.get('/api/basemap/features?layer=buildings&limit=201').status_code == 422
        assert browser.get('/api/basemap/features?layer=unknown').status_code == 422
        assert browser.get('/api/basemap/features?layer=roads&district_id=unknown').status_code == 422


def test_basemap_reads_and_live_workplace_do_not_rewrite_simulation(client):
    from app import db
    original_graph = copy.deepcopy((catalog.NODES, catalog.ROADS, catalog.ROUTES, catalog.PLACES))
    before = client.get('/api/dashboard').json()
    asset = client.get('/api/basemap').content
    assert submit(client).status_code == 201
    after_registration = client.get('/api/dashboard').json()
    assert after_registration['people'] == before['people']
    assert after_registration['clock'] == before['clock']
    with db.Session() as session:
        original_payload = copy.deepcopy(session.get(db.Simulation, 1).payload)
    assert client.get('/api/basemap').content == asset
    assert client.get('/api/basemap/metadata').json()['sha256'] == basemap.BASEMAP_SHA256
    assert any(place['id'] == 'office:aurora' for place in client.get('/api/city').json()['places'])
    assert all(place['id'] != 'office:aurora' for place in json.loads(asset)['places'])
    with db.Session() as session:
        assert session.get(db.Simulation, 1).payload == original_payload
    assert (catalog.NODES, catalog.ROADS, catalog.ROUTES, catalog.PLACES) == original_graph


def test_mcp_basemap_tools_are_read_only_and_bounded():
    import asyncio
    from app import mcp_server
    async def inspect_tools():
        tools = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
        for name in ('get_basemap_metadata', 'list_basemap_features'):
            assert tools[name].annotations.readOnlyHint is True
            assert tools[name].annotations.destructiveHint is False
        fields = tools['list_basemap_features'].inputSchema['properties']
        assert fields['limit']['maximum'] == 200
        assert fields['search']['anyOf'][0]['maxLength'] == 200
        assert fields['district_id']['anyOf'][0]['maxLength'] == 80
    asyncio.run(inspect_tools())
