"""Shared, versioned fictional spatial contract. Vendored unchanged into all services.

Canonical locations are metres in a local engineering grid. Legacy lat/lon and
flat world x/y are compatibility fields, never Earth coordinates.
"""
from copy import deepcopy
import csv
import hashlib
import heapq
import io
import json
import math
from pathlib import Path
import random
import sqlite3
import struct
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WORLD = json.loads(Path(__file__).with_name('northstar-world.json').read_text())
CRS = 'northstar-local-m-v1'
SPATIAL_VERSION = '2026.09.27.2'
REFERENCE = WORLD['spatial_reference']
LAYERS = WORLD['spatial_layers']
DISTRICTS = {d['id']:d for d in WORLD['districts']}
FACILITIES = {d['id']:d for d in WORLD['facilities']}
POLYGONS = {d['id']:d['geometry']['coordinates'][0] for d in LAYERS['districts']}
REGION = LAYERS['region']['geometry']['coordinates'][0]


def contains(ring, x, y):
    inside = False
    for (ax,ay),(bx,by) in zip(ring, ring[1:]+ring[:1]):
        dx,dy=bx-ax,by-ay
        length=math.hypot(dx,dy)
        if length and abs(dx*(y-ay)-dy*(x-ax))/length <= .001 and min(ax,bx)-.001<=x<=max(ax,bx)+.001 and min(ay,by)-.001<=y<=max(ay,by)+.001:
            return True
        if (ay>y)!=(by>y) and x<(bx-ax)*(y-ay)/(by-ay)+ax:
            inside=not inside
    return inside


class Location(BaseModel):
    model_config = ConfigDict(extra='forbid')
    crs: Literal['northstar-local-m-v1']
    units: Literal['m']
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)
    district_id: str = Field(max_length=40)
    precision: Literal['simulated_point','district_centroid','facility']
    provenance: str = Field(default='provided', min_length=1, max_length=120)
    facility_id: str | None = Field(default=None, max_length=100)

    @field_validator('x','y', mode='before')
    @classmethod
    def real_number(cls, value):
        if isinstance(value,bool) or not isinstance(value,(int,float)):
            raise ValueError('Coordinates must be finite JSON numbers in metres')
        return value

    @model_validator(mode='after')
    def valid_location(self):
        if self.district_id not in DISTRICTS:
            raise ValueError('Unknown Northstar district')
        if not contains(REGION,self.x,self.y) or not contains(POLYGONS[self.district_id],self.x,self.y):
            raise ValueError('Location must lie within its Northstar district polygon')
        if self.precision=='facility':
            site=FACILITIES.get(self.facility_id)
            if not site or site['district_id']!=self.district_id or math.hypot(self.x-site['x']*1000,self.y-site['y']*1000)>.01:
                raise ValueError('Facility precision requires its exact shared facility anchor')
        elif self.facility_id is not None:
            raise ValueError('facility_id requires facility precision')
        if self.precision=='district_centroid':
            d=DISTRICTS[self.district_id]
            if math.hypot(self.x-d['x']*1000,self.y-d['y']*1000)>.01:
                raise ValueError('District-centroid precision requires the district representative point')
        return self


def validate_location(value, district_id=None):
    location=Location.model_validate(value).model_dump(exclude_none=True)
    if district_id is not None and location['district_id']!=district_id:
        raise ValueError('Location district does not match the record district')
    return location


def legacy_location(lat,lon,district_id,precision='simulated_point',provenance='legacy_local_point'):
    return validate_location(dict(crs=CRS,units='m',x=round(lon*100000,6),y=round(lat*100000,6),
                                  district_id=district_id,precision=precision,provenance=provenance),district_id)


def district_location(district_id):
    d=DISTRICTS[district_id]
    return legacy_location(d['lat'],d['lon'],district_id,'district_centroid','district_only_record')


def facility_location(facility_id):
    f=FACILITIES[facility_id]
    return validate_location(dict(crs=CRS,units='m',x=f['x']*1000,y=f['y']*1000,
        district_id=f['district_id'],precision='facility',provenance='shared_facility_atlas',facility_id=facility_id))


def generated_location(record_id,district_id):
    rng=random.Random(int(hashlib.sha256(str(record_id).encode()).hexdigest(),16))
    d=DISTRICTS[district_id]
    for _ in range(100):
        x,y=round(d['x']*1000+rng.uniform(-1000,1000),3),round(d['y']*1000+rng.uniform(-800,800),3)
        if contains(POLYGONS[district_id],x,y):
            return validate_location(dict(crs=CRS,units='m',x=x,y=y,district_id=district_id,
                precision='simulated_point',provenance='deterministic_simulated_scene'))
    return district_location(district_id)


def spatial_layers():
    return {'world_version':WORLD['version'],'spatial_version':SPATIAL_VERSION,'synthetic':True,
            'format':'northstar-spatial-json-v1','reference':deepcopy(REFERENCE),
            'layers':deepcopy(LAYERS),
            'facilities':[{**f,'location':facility_location(f['id'])} for f in WORLD['facilities']],
            'neighbourhoods':[{**n,'location':legacy_location(n['y']/100,n['x']/100,n['parent_district_id'],provenance='shared_neighbourhood_anchor')} for n in WORLD['neighbourhoods']]}


def route(from_x,from_y,to_x,to_y):
    """Shortest-distance graph route, with explicit off-network access connectors."""
    for x,y in ((from_x,from_y),(to_x,to_y)):
        if isinstance(x,bool) or isinstance(y,bool) or not math.isfinite(x) or not math.isfinite(y) or not contains(REGION,x,y):
            raise ValueError('Route endpoints must be finite metre coordinates inside Northstar')
    points={n['id']:(n['x'],n['y']) for n in LAYERS['road_nodes']}
    roads=LAYERS['roads']; projections={}
    for label,(x,y) in [('start',(from_x,from_y)),('end',(to_x,to_y))]:
        candidates=[]
        for edge in roads:
            ax,ay=points[edge['from']];bx,by=points[edge['to']]
            t=max(0,min(1,((x-ax)*(bx-ax)+(y-ay)*(by-ay))/((bx-ax)**2+(by-ay)**2)))
            px,py=ax+t*(bx-ax),ay+t*(by-ay)
            candidates.append((math.hypot(x-px,y-py),edge['id'],t,px,py))
        distance,edge_id,t,px,py=min(candidates)
        projections[label]=(edge_id,t)
        points[label]=(px,py);points[label+'_point']=(x,y)
    graph={key:[] for key in points}
    def connect(a,b,speed,edge_id,access=False):
        distance=math.dist(points[a],points[b]);seconds=distance/(speed/3.6)
        graph[a].append((b,distance,seconds,edge_id,access));graph[b].append((a,distance,seconds,edge_id,access))
    for edge in roads:
        split=[(0,edge['from']),(1,edge['to'])]+[(t,label) for label,(ident,t) in projections.items() if ident==edge['id']]
        split.sort()
        for (_,a),(_,b) in zip(split,split[1:]):connect(a,b,edge['speed_kph'],edge['id'])
    connect('start_point','start',15,'access_start',True);connect('end','end_point',15,'access_end',True)
    best={'start_point':0};previous={};queue=[(0,'start_point')]
    while queue:
        distance,node=heapq.heappop(queue)
        if distance!=best.get(node):continue
        if node=='end_point':break
        for target,length,seconds,edge_id,access in graph[node]:
            new=distance+length
            if new<best.get(target,math.inf):
                best[target]=new;previous[target]=(node,length,seconds,edge_id,access);heapq.heappush(queue,(new,target))
    steps=[];cursor='end_point'
    while cursor!='start_point':
        prev,length,seconds,edge_id,access=previous[cursor]
        steps.append((prev,cursor,length,seconds,edge_id,access));cursor=prev
    steps.reverse()
    geometry=[list(points['start_point'])]+[list(points[s[1]]) for s in steps]
    return {'synthetic':True,'crs':CRS,'units':'m','spatial_version':SPATIAL_VERSION,
        'method':'shortest_distance_on_authored_road_graph','distance_m':round(best['end_point'],3),
        'network_distance_m':round(sum(s[2] for s in steps if not s[5]),3),
        'off_network_distance_m':round(sum(s[2] for s in steps if s[5]),3),
        'estimated_travel_seconds':round(sum(s[3] for s in steps),3),
        'road_ids':list(dict.fromkeys(s[4] for s in steps if not s[5])),
        'geometry':{'type':'LineString','coordinates':geometry},
        'notes':['Authored road speeds and 15 km/h off-network access assumptions; no live traffic.',
                 'This research estimate does not change or describe actual simulation response-clock milestones.']}


def geometry_wkt(geometry):
    number=lambda p:' '.join(format(n,'.9g') for n in p)
    if geometry['type']=='Point':return 'POINT ('+number(geometry['coordinates'])+')'
    if geometry['type']=='LineString':return 'LINESTRING ('+', '.join(number(p) for p in geometry['coordinates'])+')'
    return 'POLYGON ('+', '.join('('+', '.join(number(p) for p in ring)+')' for ring in geometry['coordinates'])+')'


def features():
    yield 'region', 'northstar', '', LAYERS['region']['geometry'], {'name':WORLD['name']}
    for d in LAYERS['districts']:yield 'districts',d['id'],d['id'],d['geometry'],{'name':d['name']}
    for f in WORLD['facilities']:yield 'facilities',f['id'],f['district_id'],{'type':'Point','coordinates':[f['x']*1000,f['y']*1000]},f
    for n in WORLD['neighbourhoods']:yield 'neighbourhoods',n['id'],n['parent_district_id'],{'type':'Point','coordinates':[n['x']*1000,n['y']*1000]},n
    for r in LAYERS['roads']:yield 'roads',r['id'],'',r['geometry'],{k:v for k,v in r.items() if k!='geometry'}


def layer_csv():
    output=io.StringIO();writer=csv.writer(output)
    writer.writerow(['layer','id','district_id','crs','units','geometry_wkt','properties_json'])
    for layer,ident,district,geometry,properties in features():
        writer.writerow([layer,ident,district,CRS,'m',geometry_wkt(geometry),json.dumps(properties,sort_keys=True)])
    return output.getvalue()


def _wkb(geometry):
    kind=geometry['type'];coords=geometry['coordinates'];header=struct.pack('<BI',1,{'Point':1,'LineString':2,'Polygon':3}[kind])
    if kind=='Point':return header+struct.pack('<dd',*coords)
    if kind=='LineString':return header+struct.pack('<I',len(coords))+b''.join(struct.pack('<dd',*p) for p in coords)
    return header+struct.pack('<I',len(coords))+b''.join(struct.pack('<I',len(ring))+b''.join(struct.pack('<dd',*p) for p in ring) for ring in coords)


def geopackage():
    """OGC GeoPackage feature layers with a declared local Cartesian metre CRS."""
    db=sqlite3.connect(':memory:')
    db.executescript('''PRAGMA application_id=1196444487; PRAGMA user_version=10300;
    CREATE TABLE gpkg_spatial_ref_sys(srs_name TEXT NOT NULL,srs_id INTEGER NOT NULL PRIMARY KEY,organization TEXT NOT NULL,organization_coordsys_id INTEGER NOT NULL,definition TEXT NOT NULL,description TEXT);
    CREATE TABLE gpkg_contents(table_name TEXT NOT NULL PRIMARY KEY,data_type TEXT NOT NULL,identifier TEXT UNIQUE,description TEXT DEFAULT '',last_change DATETIME NOT NULL DEFAULT '2026-09-27T00:00:00.000Z',min_x DOUBLE,min_y DOUBLE,max_x DOUBLE,max_y DOUBLE,srs_id INTEGER,FOREIGN KEY(srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id));
    CREATE TABLE gpkg_geometry_columns(table_name TEXT NOT NULL,column_name TEXT NOT NULL,geometry_type_name TEXT NOT NULL,srs_id INTEGER NOT NULL,z TINYINT NOT NULL,m TINYINT NOT NULL,PRIMARY KEY(table_name,column_name),FOREIGN KEY(table_name) REFERENCES gpkg_contents(table_name),FOREIGN KEY(srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id));''')
    wgs='GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]'
    db.executemany('INSERT INTO gpkg_spatial_ref_sys VALUES(?,?,?,?,?,?)',[
        ('Undefined Cartesian',-1,'NONE',-1,'undefined',''),('Undefined geographic',0,'NONE',0,'undefined',''),
        ('WGS 84',4326,'EPSG',4326,wgs,'Required definition; not used by Northstar layers'),
        (REFERENCE['name'],100001,'NONE',100001,REFERENCE['wkt'],REFERENCE['description'])])
    types={'region':'POLYGON','districts':'POLYGON','facilities':'POINT','neighbourhoods':'POINT','roads':'LINESTRING'}
    for table,kind in types.items():
        db.execute(f'CREATE TABLE {table}(fid INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,district_id TEXT,properties_json TEXT,geom {kind})')
        db.execute('INSERT INTO gpkg_contents(table_name,data_type,identifier,description,srs_id) VALUES(?,?,?,?,?)',(table,'features',table,WORLD['description'],100001))
        db.execute('INSERT INTO gpkg_geometry_columns VALUES(?,?,?,?,?,?)',(table,'geom',kind,100001,0,0))
    for table,ident,district,geometry,properties in features():
        blob=b'GP'+bytes([0,1])+struct.pack('<i',100001)+_wkb(geometry)
        db.execute(f'INSERT INTO {table}(id,district_id,properties_json,geom) VALUES(?,?,?,?)',(ident,district,json.dumps(properties),blob))
    db.commit();result=db.serialize();db.close();return result


def attach_spatial_routes(app):
    from fastapi import HTTPException
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import Response, JSONResponse
    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(request, exc):
        # JSON permits neither NaN nor infinity; never echo a rejected value
        # back through the JSON encoder (which would turn a 422 into a 500).
        return JSONResponse(status_code=422, content={'detail':[
            {key:error[key] for key in ('type','loc','msg') if key in error}
            for error in exc.errors()]})
    @app.get('/api/spatial/layers',tags=['Spatial'])
    def get_spatial_layers():return spatial_layers()
    @app.get('/api/spatial/route',tags=['Spatial'])
    def get_spatial_route(from_x:float,from_y:float,to_x:float,to_y:float):
        try:return route(from_x,from_y,to_x,to_y)
        except ValueError as exc:raise HTTPException(422,str(exc)) from None
    @app.get('/api/spatial/export.csv',tags=['Spatial'])
    def export_spatial_csv():
        return Response(layer_csv(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="northstar-spatial.csv"'})
    @app.get('/api/spatial/export.gpkg',tags=['Spatial'])
    def export_spatial_gpkg():
        return Response(geopackage(),media_type='application/geopackage+sqlite3',headers={'Content-Disposition':'attachment; filename="northstar-spatial.gpkg"'})
