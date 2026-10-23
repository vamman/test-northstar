"""Read-only MCP. This process has no database, control, report, or upstream credentials."""
from __future__ import annotations

import os
from typing import Annotated, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.responses import JSONResponse

BASE = os.getenv('API_BASE_URL', 'http://127.0.0.1:8825').rstrip('/')
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
ID = Annotated[str, Field(min_length=1, max_length=120, pattern=r'^[A-Za-z0-9_:-]+$')]
LIMIT = Annotated[int, Field(ge=1, le=200)]
OFFSET = Annotated[int, Field(ge=0)]


def allow(name, defaults):
    return defaults + [x.strip() for x in os.getenv(name, '').split(',') if x.strip()]


mcp = FastMCP('Northstar Transit', host=os.getenv('MCP_HOST', '127.0.0.1'),
    port=int(os.getenv('MCP_PORT', '8826')), stateless_http=True, json_response=True,
    streamable_http_path='/mcp',
    instructions='Observe the synthetic Northstar transit simulation. All tools are read-only. '
        'Start with get_transit_status, get_transit_dashboard and get_transit_model. '
        'Each dot represents one modeled traveler, not the entire regional population. '
        'June through September is the authored cruise season. Calendar and demand are synthetic. '
        'Coordinates are local metres, never GPS. Separate requests may observe different times. '
        'Keep transit simulation time, source simulator time, and wall observation time distinct. '
        'A delivered outbox job means 911 accepted a call, not that responders arrived. '
        'A hospital journey completion does not resolve the overall city incident. '
        'Road vehicle marks represent weighted traffic cohorts; never add them to individual traveler counts. '
        'Utility coefficients are authored; cumulative meters begin at installed_at with no historical backfill. '
        'Read pagination totals/offsets, and use reports to inspect authoritative cross-service feedback.',
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
        allowed_hosts=allow('MCP_ALLOWED_HOSTS', ['127.0.0.1:*', 'localhost:*', '[::1]:*']),
        allowed_origins=allow('MCP_ALLOWED_ORIGINS', ['http://127.0.0.1:*', 'http://localhost:*', 'http://[::1]:*'])))


async def get(path, **params):
    try:
        async with httpx.AsyncClient(base_url=BASE, timeout=30, trust_env=False, follow_redirects=False) as client:
            response = await client.get(path, params={k: v for k, v in params.items() if v is not None})
            response.raise_for_status()
            result = response.json()
            return result if isinstance(result, dict) else {'items': result}
    except httpx.HTTPStatusError as exc:
        raise ValueError(f'Transit API returned HTTP {exc.response.status_code}. List valid IDs first.') from None
    except httpx.RequestError:
        raise RuntimeError('Transit API unavailable. Check the transit API service.') from None


@mcp.tool(annotations=READ)
async def get_transit_status() -> dict:
    """Database, worker heartbeat, world version, run identity and current clock."""
    return await get('/api/health')


@mcp.tool(annotations=READ)
async def get_transit_dashboard() -> dict:
    """Consistent operations summary: counts, mode demand, arrivals, alarms and active events (no bulk people)."""
    result = await get('/api/dashboard')
    result.pop('people', None)
    result.pop('vehicles', None)
    return result


@mcp.tool(annotations=READ)
async def get_transit_model() -> dict:
    """Model assumptions, limits, sampled-person definition and references."""
    return await get('/api/model')


@mcp.tool(annotations=READ)
async def get_city_network() -> dict:
    """Shared anchors, transport stops, explicit route paths and capacities; omit decorative building footprints."""
    result = await get('/api/city')
    result.pop('buildings', None)
    result.pop('roads', None)
    return result


@mcp.tool(annotations=READ)
async def get_basemap_metadata() -> dict:
    """Shared immutable detailed-map version, hash, counts, full extent and routing limits."""
    return await get('/api/basemap/metadata')


@mcp.tool(annotations=READ)
async def list_basemap_features(layer: Literal['roads', 'road_nodes', 'buildings', 'places', 'districts', 'water'],
                               limit: LIMIT = 50, offset: OFFSET = 0,
                               district_id: Annotated[str | None, Field(max_length=80)] = None,
                               search: Annotated[str | None, Field(max_length=200)] = None) -> dict:
    """Page through exact shared basemap features in local metres, with provenance and stable IDs."""
    return await get('/api/basemap/features', layer=layer, limit=limit, offset=offset, district_id=district_id, search=search)


@mcp.tool(annotations=READ)
async def get_transit_world() -> dict:
    """Canonical Northstar atlas shared with sibling simulators."""
    return await get('/api/world')


@mcp.tool(annotations=READ)
async def list_transit_routes() -> dict:
    """Subway, bus, rail, ferry and other services with stops, headways, capacities and local geometry."""
    return await get('/api/routes')


@mcp.tool(annotations=READ)
async def list_city_events(month: Annotated[int | None, Field(ge=1, le=12)] = None) -> dict:
    """Annual city event calendar with demand, venue, times and seasonal cruise window."""
    return await get('/api/events', month=month)


@mcp.tool(annotations=READ)
async def list_transit_arrivals() -> dict:
    """Scheduled and actual simulated arrivals at airport, cruise terminal and rail interchange."""
    return await get('/api/arrivals')


@mcp.tool(annotations=READ)
async def list_smart_sensors() -> dict:
    """Activity-derived smart-city readings, units, timestamps and alarm state."""
    return await get('/api/sensors')


@mcp.tool(annotations=READ)
async def get_transit_infrastructure() -> dict:
    """Power, elevators, internet, water and sewer: loads, capacities, cumulative meters and outages."""
    return await get('/api/infrastructure')


@mcp.tool(annotations=READ)
async def get_transit_facility_utilities(facility_id: ID) -> dict:
    """One facility's detailed utility readings and elevator assets. Obtain IDs from get_transit_infrastructure."""
    return await get(f'/api/infrastructure/facilities/{facility_id}')


@mcp.tool(annotations=READ)
async def get_road_traffic(limit: LIMIT = 50, offset: OFFSET = 0) -> dict:
    """Work/school demand, congestion and paginated weighted vehicle cohorts, separate from individual people."""
    return await get('/api/road-traffic', limit=limit, offset=offset)


@mcp.tool(annotations=READ)
async def list_transit_incidents() -> dict:
    """Current run incidents, disruption effects and dispatch linkage."""
    return await get('/api/incidents')


@mcp.tool(annotations=READ)
async def list_travelers(mode: Annotated[str | None, Field(max_length=30)] = None,
    status: Annotated[str | None, Field(max_length=30)] = None, limit: LIMIT = 50, offset: OFFSET = 0) -> dict:
    """Page through active synthetic people; origins, destinations and actual movement/queue state."""
    return await get('/api/people', mode=mode, status=status, limit=limit, offset=offset)


@mcp.tool(annotations=READ)
async def list_transit_vehicles(mode: Annotated[str | None, Field(max_length=30)] = None,
    route_id: ID | None = None, limit: LIMIT = 50, offset: OFFSET = 0) -> dict:
    """Page through vehicles, positions, occupancy, capacity and service state; includes schematic gateway movements."""
    return await get('/api/vehicles', mode=mode, route_id=route_id, limit=limit, offset=offset)


@mcp.tool(annotations=READ)
async def get_transit_connections() -> dict:
    """911 dispatch delivery state, hospital observations and recent source-clock-preserving feedback."""
    return await get('/api/integrations/status')


@mcp.tool(annotations=READ)
async def list_simulator_reports(source: Annotated[str | None, Field(max_length=80)] = None,
    incident_id: ID | None = None, limit: LIMIT = 50, offset: OFFSET = 0) -> dict:
    """Persistent idempotent reports from 911, hospital or other producers, including old runs."""
    return await get('/api/integrations/reports', source=source, incident_id=incident_id, limit=limit, offset=offset)


@mcp.custom_route('/health', methods=['GET'])
async def health(request):
    return JSONResponse({'status': 'ok', 'synthetic': True, 'service': 'northstar-transit-mcp'})




@mcp.tool(annotations=READ)
async def get_office_workplaces() -> dict:
    """Registered office destinations, tenant context, source clocks and observed commuting counts."""
    return await get('/api/workplaces')


@mcp.tool(annotations=READ)
async def list_office_commutes(workplace_id: ID | None = None, worker_id: ID | None = None,
    direction: Literal['outbound','return'] | None = None, status: str | None = None,
    limit: LIMIT = 100, offset: OFFSET = 0) -> dict:
    """Actual office journeys, deferred requests, recent completed legs and bounded contact exposure. Consult get_shared_clock for current alignment; historical source times remain distinct."""
    return await get('/api/commutes',workplace_id=workplace_id,worker_id=worker_id,direction=direction,status=status,limit=limit,offset=offset)




@mcp.tool(annotations=READ)
async def get_environment_impact() -> dict:
    """Read the last factors actually applied by this simulator, source revision and local clock."""
    return await get('/api/environment')

@mcp.tool(annotations=READ)
async def get_shared_clock() -> dict:
    """Read shared time, pause/pace, lag and cutover provenance. Consult this before interpreting older independent-clock descriptions; historical source times remain distinct."""
    return await get('/api/clock')

if __name__ == '__main__':
    mcp.run(transport='streamable-http')
