"""Immutable detailed Northstar display geography; no database or upstream I/O.

Vendored with northstar-basemap.json. Operational routing remains service-owned.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Literal

BASEMAP_PATH = Path(__file__).with_name('northstar-basemap.json')
BASEMAP_BYTES = BASEMAP_PATH.read_bytes()
BASEMAP_SHA256 = hashlib.sha256(BASEMAP_BYTES).hexdigest()
BASEMAP = json.loads(BASEMAP_BYTES)
if BASEMAP['schema_version'] != 'northstar.basemap.v1':
    raise RuntimeError('Unsupported detailed basemap schema')
if hashlib.sha256(Path(__file__).with_name('northstar-world.json').read_bytes()).hexdigest() != BASEMAP['world_sha256']:
    raise RuntimeError('Detailed basemap and shared atlas differ; publish a matching release')
LAYERS = ('roads', 'road_nodes', 'buildings', 'places', 'districts', 'water')


def metadata():
    keys = ('schema_version', 'id', 'name', 'version', 'synthetic', 'world_version', 'world_sha256', 'crs', 'units',
            'reference', 'timezone', 'bounds', 'views', 'counts', 'sources', 'publication', 'routing', 'notes')
    return {**{key: deepcopy(BASEMAP[key]) for key in keys}, 'sha256': BASEMAP_SHA256,
            'byte_length': len(BASEMAP_BYTES), 'endpoint': '/api/basemap',
            'feature_endpoint': '/api/basemap/features', 'layers': list(LAYERS)}


def feature_page(layer, *, limit=50, offset=0, district_id=None, search=None):
    if layer not in LAYERS:
        raise ValueError('Unknown basemap layer')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200 or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError('limit must be 1–200 and offset must be nonnegative')
    if district_id and district_id not in {district['id'] for district in BASEMAP['districts']}:
        raise ValueError('Unknown basemap district')
    rows = BASEMAP[layer]
    if district_id:
        rows = [row for row in rows if row.get('district_id') == district_id or district_id in row.get('district_ids', []) or (layer == 'districts' and row['id'] == district_id)]
    if search:
        term = search.casefold()
        rows = [row for row in rows if term in ' '.join(str(row.get(key, '')) for key in ('id', 'name', 'kind', 'class')).casefold()]
    return {'schema_version': BASEMAP['schema_version'], 'basemap_version': BASEMAP['version'],
            'world_version': BASEMAP['world_version'], 'sha256': BASEMAP_SHA256, 'synthetic': True,
            'crs': BASEMAP['crs'], 'units': BASEMAP['units'], 'layer': layer,
            'items': deepcopy(rows[offset:offset + limit]), 'total': len(rows), 'limit': limit, 'offset': offset}


def attach_basemap_routes(app):
    from fastapi import HTTPException, Query
    from fastapi.responses import Response

    @app.get('/api/basemap', tags=['Basemap'])
    def get_basemap():
        return Response(BASEMAP_BYTES, media_type='application/json', headers={
            'ETag': '"' + BASEMAP_SHA256 + '"', 'X-Basemap-Version': BASEMAP['version'], 'X-Content-SHA256': BASEMAP_SHA256})

    @app.get('/api/basemap/metadata', tags=['Basemap'])
    def get_basemap_metadata():
        return metadata()

    @app.get('/api/basemap/features', tags=['Basemap'])
    def get_basemap_features(layer: Literal['roads', 'road_nodes', 'buildings', 'places', 'districts', 'water'],
                            limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0, le=10000000),
                            district_id: str | None = Query(default=None, max_length=80),
                            search: str | None = Query(default=None, max_length=200)):
        try:
            return feature_page(layer, limit=limit, offset=offset, district_id=district_id, search=search)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
