"""Authored Northstar transport geography; all distances are local-grid metres.

The shared regional atlas is the authority for anchors and district polygons.
The local streets, buildings, routes, timetable and event calendar are synthetic.
"""
from pathlib import Path
import heapq
import json
import math
import random

WORLD = json.loads(Path(__file__).with_name('northstar-world.json').read_text())
CRS = 'northstar-local-m-v1'
COLORS = {'subway':'#4ee1ba','bus':'#ffc46b','rail':'#ae9fff','ferry':'#69c9ff',
          'walk':'#d6edf3','cycle':'#a6e987','taxi':'#ffd768','car':'#d68e79','aircraft':'#e1ebff','cruise':'#79cfff'}
BOUNDS = {'min_x':-14300,'min_y':3100,'max_x':29400,'max_y':26300}
DISTRICTS = WORLD['spatial_layers']['districts']


def contains(ring, x, y):
    inside = False
    for (ax, ay), (bx, by) in zip(ring, ring[1:] + ring[:1]):
        if (ay > y) != (by > y) and x < (bx-ax)*(y-ay)/(by-ay)+ax:
            inside = not inside
    return inside


def district_at(x, y):
    for district in DISTRICTS:
        if contains(district['geometry']['coordinates'][0], x, y):
            return district['id']
    raise ValueError(f'Point outside Northstar districts: {x}, {y}')


def in_bounds(x,y):
    return BOUNDS['min_x'] <= x <= BOUNDS['max_x'] and BOUNDS['min_y'] <= y <= BOUNDS['max_y']


PLACES = []
for facility in WORLD['facilities']:
    x,y = facility['x']*1000,facility['y']*1000
    if in_bounds(x,y):
        PLACES.append({**facility,'x':x,'y':y,'facility_id':facility['id']})
for district in WORLD['districts']:
    if in_bounds(district['x']*1000,district['y']*1000):
        PLACES.append({'id':district['id']+'_centre','name':district['name'], 'kind':'district_centre',
                       'x':district['x']*1000,'y':district['y']*1000,'district_id':district['id']})
for neighbourhood in WORLD['neighbourhoods']:
    if in_bounds(neighbourhood['x']*1000,neighbourhood['y']*1000):
        PLACES.append({'id':neighbourhood['id'],'name':neighbourhood['name'],'kind':'residential',
                       'x':neighbourhood['x']*1000,'y':neighbourhood['y']*1000,
                       'district_id':neighbourhood['parent_district_id']})

VENUES = [
 ('central_station','Central Exchange','station',1800,9100),
 ('civic_square','Civic Square','civic',1200,9300),
 ('convention_centre','Northstar Convention Centre','convention',3200,9000),
 ('stadium','Aurora Stadium','stadium',-1200,13200),
 ('university','Northstar University','university',-3500,12900),
 ('riverfront','Riverfront Festival Park','park',5200,8200),
 ('old_town','Old Town Market','market',-600,8200),
 ('arts_centre','Northstar Arts Centre','culture',1800,10400),
 ('science_centre','Discovery Science Centre','culture',10500,9400),
 ('east_exchange','East River Exchange','station',12100,9500),
 ('west_foundry','Foundry Works','employment',-8200,9800),
 ('west_market','Westside Market','market',-4200,9600),
 ('south_exchange','South Bank Exchange','station',-1400,5500),
 ('sports_park','South Bank Sports Park','park',900,5500),
 ('marina','Cove Marina','ferry_terminal',24800,22300),
 ('harbour_market','Harbour Market & Boardwalk','market',26300,23400),
 ('coastal_beach','Bay Reach Beach','park',25500,25100),
 ('river_pier','East River Pier','ferry_terminal',14100,11500),
 ('innovation_park','East Innovation Park','employment',14000,8500),
 ('airport_hotel','Aviation Hotel District','hotel',15100,10500),
 ('central_hotels','Central Hotel Quarter','hotel',2900,9700),
 ('north_depot','North Commons Mobility Hub','station',-3600,14700),
]
for ident,name,kind,x,y in VENUES:
    PLACES.append({'id':ident,'name':name,'kind':kind,'x':x,'y':y,'district_id':district_at(x,y)})

# Keep atlas links intact and add a visibly dense, connected local street network.
NODES = {n['id']:(n['x'],n['y']) for n in WORLD['spatial_layers']['road_nodes']}
ROADS = [{**r,'class':'arterial'} for r in WORLD['spatial_layers']['roads']]
_rng = random.Random(1701)
BUILDINGS=[]
CLUSTERS = [('central',1800,9100,15,13,360),('north',-3600,13500,9,7,440),
            ('west',-10700,10100,9,7,420),('east',12100,9500,11,9,430),
            ('south',-1400,5500,9,7,390),('coastal',25900,23500,9,7,380)]
STREET_NAMES=['Aster','Birch','Cedar','Discovery','Elm','Foundry','Garden','Harbour','Iris','Juniper','King','Linden','Maple','Northstar','Orchard','Park','Queen','River','Spruce','Union']


def _edge(a,b,name,category='local'):
    start,end=NODES[a],NODES[b]
    ROADS.append({'id':f'local_{len(ROADS):04d}','name':name,'from':a,'to':b,'class':category,
                  'bidirectional':True,'speed_kph':30 if category=='local' else 45,
                  'length_m':round(math.dist(start,end),3),
                  'geometry':{'type':'LineString','coordinates':[list(start),list(end)]}})


for district,cx,cy,nx,ny,spacing in CLUSTERS:
    previous_nodes=list(NODES)
    for ix in range(nx):
        for iy in range(ny):
            node=f'{district}_{ix}_{iy}'
            x,y=cx+(ix-(nx-1)/2)*spacing,cy+(iy-(ny-1)/2)*spacing
            NODES[node]=(x,y)
            if ix: _edge(f'{district}_{ix-1}_{iy}',node,f'{STREET_NAMES[iy]} Street')
            if iy: _edge(f'{district}_{ix}_{iy-1}',node,f'{STREET_NAMES[ix]} Avenue')
            if ix<nx-1 and iy<ny-1:
                # Four distinct structures per block with a 45 m street setback.
                for bx,by in [(0,0),(1,0),(0,1),(1,1)]:
                    width=_rng.uniform(spacing*.20,spacing*.32)
                    height=_rng.uniform(spacing*.18,spacing*.32)
                    BUILDINGS.append({'id':f'building_{len(BUILDINGS):04d}',
                        'x':round(x+45+bx*spacing*.44),'y':round(y+45+by*spacing*.44),
                        'width':round(width),'height':round(height),
                        'kind':_rng.choices(['residential','commercial','office','civic'],[5,3,3 if district=='central' else 1,1])[0],
                        'height_floors':_rng.randint(3,18) if district=='central' else _rng.randint(1,7),
                        'district_id':district})
    mid=f'{district}_{nx//2}_{ny//2}'
    nearest=min(previous_nodes,key=lambda n:math.dist(NODES[n],NODES[mid]))
    _edge(mid,nearest,f'{district.title()} Boulevard','collector')

# Add explicit access links, shared by walking, cycling, taxis and bus routes.
for place in PLACES:
    node='place:'+place['id']
    xy=(place['x'],place['y'])
    nearest=min(NODES,key=lambda n:math.dist(NODES[n],xy))
    NODES[node]=xy
    _edge(node,nearest,place['name']+' Access','access')

PLACE_INDEX={p['id']:p for p in PLACES}
GRAPH={n:[] for n in NODES}
for road in ROADS:
    length=math.dist(NODES[road['from']],NODES[road['to']])
    GRAPH[road['from']].append((road['to'],length))
    GRAPH[road['to']].append((road['from'],length))
_PATH_CACHE={}


def road_path(origin_id,destination_id):
    """Shortest-distance path on the explicit, connected bidirectional graph."""
    key=(origin_id,destination_id)
    if key in _PATH_CACHE: return _PATH_CACHE[key]
    start,finish='place:'+origin_id,'place:'+destination_id
    distances={start:0.0};previous={};heap=[(0.,start)]
    while heap:
        distance,node=heapq.heappop(heap)
        if node==finish: break
        if distance!=distances.get(node):continue
        for target,length in GRAPH[node]:
            candidate=distance+length
            if candidate<distances.get(target,math.inf):
                distances[target]=candidate;previous[target]=node
                heapq.heappush(heap,(candidate,target))
    cursor=finish;ids=[cursor]
    while cursor!=start:
        cursor=previous[cursor];ids.append(cursor)
    points=[]
    for node in reversed(ids):
        point=list(NODES[node])
        if not points or point!=points[-1]:points.append(point)
    if len(points)==1:points.append(points[0][:])
    result={'points':points,'length':distances[finish]}
    _PATH_CACHE[key]=result
    return result


ROUTES=[]


def add_route(ident,name,mode,stops,capacity,headway,speed):
    points=[];stop_distances=[0.];length=0.
    for origin,destination in zip(stops,stops[1:]):
        if mode in ('bus',):
            segment=road_path(origin,destination)['points']
        else:
            # Dedicated rail track or navigable synthetic channel, independent of streets.
            a,b=PLACE_INDEX[origin],PLACE_INDEX[destination]
            segment=[[a['x'],a['y']],[b['x'],b['y']]]
        for point in segment:
            if points and point!=points[-1]:length+=math.dist(points[-1],point)
            if not points or point!=points[-1]:points.append(point)
        stop_distances.append(length)
    ROUTES.append({'id':ident,'name':name,'mode':mode,'color':COLORS[mode],'stops':stops,
                   'points':points,'capacity':capacity,'headway_minutes':headway,'speed_mps':speed,
                   'stop_distances':stop_distances,'length_m':length,'service_start_hour':5,'service_end_hour':25})


add_route('metro_1','M1 · Northstar Line','subway',['northstar_rail','west_foundry','west_market','central_station','science_centre','east_exchange','northstar_airport'],420,6,16)
add_route('bus_airport','A1 · Airport Express','bus',['central_station','convention_centre','east_exchange','airport_hotel','northstar_airport'],65,10,10)
add_route('bus_coast','C2 · Coastal Connector','bus',['east_exchange','northstar_airport','river_pier','northstar_harbour','harbour_market','coastal_beach'],65,12,11)
add_route('bus_north','N3 · University & Stadium','bus',['central_station','civic_square','old_town','university','north_centre','stadium','north_depot'],65,8,9)
add_route('bus_south','S4 · South Bank','bus',['central_station','old_town','valley_green','south_exchange','sports_park','riverfront'],65,10,9)
add_route('bus_west','W5 · Foundry Shuttle','bus',['central_station','west_market','west_foundry','west_centre','northstar_rail'],55,12,9)
add_route('rail_regional','R1 · Regional Rail','rail',['northstar_rail','central_station','east_exchange','northstar_airport','northstar_harbour'],280,30,20)
add_route('ferry_harbour','F1 · Harbour Waterbus','ferry',['river_pier','marina','northstar_harbour'],100,20,8)
ROUTE_INDEX={r['id']:r for r in ROUTES}

# Authored annual recurring demand. Attendance describes a scenario, never a census.
_EVENT_ROWS=[
 ('new_year','New Year Harbour Lights',1,1,18,5,9000,'harbour_market','festival'),
 ('winter_games','Northstar Winter Games',1,24,10,9,6500,'stadium','sport'),
 ('ice_festival','Ice & Lantern Festival',2,7,14,7,7000,'civic_square','festival'),
 ('innovation_forum','Atlantic Innovation Forum',2,19,8,10,2600,'convention_centre','conference'),
 ('spring_hockey','Spring Hockey Championship',3,14,12,9,8500,'stadium','sport'),
 ('arts_week','Arts Week Opening',3,26,17,6,3100,'arts_centre','culture'),
 ('city_marathon','Northstar City Marathon',4,12,7,7,12000,'riverfront','sport'),
 ('earth_day','Earth Day Mobility Fair',4,22,10,8,4200,'civic_square','community'),
 ('graduation','University Graduation',5,16,9,8,6000,'university','education'),
 ('harbour_opening','Harbour Season Opening',5,30,11,9,7800,'harbour_market','festival'),
 ('cruise_welcome','First Cruise Welcome',6,2,8,9,2200,'northstar_harbour','cruise'),
 ('summer_solstice','Solstice River Sessions',6,21,15,9,15000,'riverfront','music'),
 ('canada_day','Northstar Canada Day',7,1,10,13,22000,'civic_square','festival'),
 ('harbour_market_day','Harbour Summer Market',7,18,8,10,8400,'harbour_market','market'),
 ('jazz_weekend','Riverfront Jazz Weekend',7,18,11,12,14000,'riverfront','music'),
 ('stadium_final','Northstar Cup Final',7,18,17,5,18000,'stadium','sport'),
 ('coastal_regatta','Coastal Regatta',8,8,9,10,10500,'marina','sport'),
 ('pride','Northstar Pride',8,15,11,11,19000,'civic_square','festival'),
 ('food_festival','Taste of Northstar',8,29,11,10,11500,'old_town','festival'),
 ('welcome_week','University Welcome Week',9,5,8,12,9000,'university','education'),
 ('ocean_congress','Atlantic Ocean Congress',9,17,8,10,4200,'convention_centre','conference'),
 ('last_sail','Cruise Season Farewell',9,28,10,8,3600,'harbour_market','cruise'),
 ('autumn_run','Autumn River Run',10,11,8,6,7200,'riverfront','sport'),
 ('harvest','Harvest Night Market',10,24,15,8,6500,'old_town','market'),
 ('remembrance','Remembrance Gathering',11,11,9,5,2200,'civic_square','community'),
 ('science_expo','Northstar Science Expo',11,21,9,9,5600,'science_centre','education'),
 ('winter_market','Winter Makers Market',12,12,10,10,12500,'old_town','market'),
 ('new_year_eve','New Year’s Eve Countdown',12,31,19,6,16000,'civic_square','festival'),
]
EVENTS=[{'id':i,'name':n,'month':m,'day':d,'start_hour':h,'duration_hours':duration,
         'attendance':attendance,'venue_id':venue,'kind':kind,'demand_window_minutes':120}
        for i,n,m,d,h,duration,attendance,venue,kind in _EVENT_ROWS]
SENSORS=[]
for place_id in ['central_station','northstar_airport','northstar_harbour','northstar_rail','east_exchange',
                 'stadium','riverfront','civic_square','university','old_town','harbour_market','south_exchange',
                 'west_market','north_depot','river_pier','convention_centre']:
    place=PLACE_INDEX[place_id]
    for kind,unit in [('footfall','people'),('queue','people'),('air_quality','µg/m³'),('noise','dBA'),('traffic','%')]:
        SENSORS.append({'id':f'{place_id}:{kind}','name':place['name']+' · '+kind.replace('_',' ').title(),
                        'place_id':place_id,'x':place['x'],'y':place['y'],'kind':kind,'unit':unit})
CITY={'id':'northstar_transit','name':'Northstar','synthetic':True,'world_version':WORLD['version'],
      'crs':CRS,'units':'m','timezone':'America/Moncton','bounds':BOUNDS,
      'places':PLACES,'roads':[r for r in ROADS if all(in_bounds(*p) for p in r['geometry']['coordinates'])],
      'buildings':BUILDINGS,'routes':ROUTES,'events':EVENTS,'sensors':SENSORS,
      'districts':DISTRICTS,'region':WORLD['spatial_layers']['region'],
      'water':[{'id':'harbour_channel','name':'Northstar Inlet','geometry':{'type':'Polygon','coordinates':[
          [[16200,14300],[20400,17300],[23700,20400],[28500,22000],[29400,26000],[29400,26500],
           [26900,26300],[27100,23500],[24800,21900],[22000,18500],[17900,15700],[16200,14300]]]}}],
      'model_note':'Authored synthetic transport model. Each dot is one modeled person; demand is a bounded sample, not a city census.',
      'cruise_season':{'start_month':6,'end_month':9,'label':'June–September'},'mode_colors':COLORS}
