"""Deterministic, bounded, JSON-persistable synthetic transport simulation.

Time is stored as UTC epoch seconds and rendered using America/Moncton. Calling
advance supplies simulated seconds; paused/speed are controls for the caller.
No network I/O, database access, threads, or wall-clock randomness during motion.
"""
from collections import Counter
from datetime import datetime, timedelta
from functools import lru_cache
import bisect
import math
from uuid import uuid4
from zoneinfo import ZoneInfo

from app import environment_client, environment_effects

from . import commutes, cruise_terminal, weather_response
from .infrastructure import (ensure_infrastructure, advance_infrastructure,
                             infrastructure_snapshot, transport_service_factor)
from .catalog import (CITY, COLORS, EVENTS, PLACE_INDEX, PLACES, ROADS, ROUTES,
                      ROUTE_INDEX, SENSORS, road_path)

TZ=ZoneInfo('America/Moncton')
DEFAULT_START='2026-07-18T08:00:00-03:00'
MAX_PEOPLE=5000
STEP_SECONDS=10
STATUS_ORDER={'reported':0,'received':1,'accepted':1,'dispatched':2,'dispatching':2,
              'on_scene':3,'transporting':4,'stabilized':4,'resolved':5,'closed':5}
INCIDENT_KINDS={'collision','medical','crowd_surge','signal_failure','storm'}
SPEEDS={'walk':1.35,'cycle':4.8,'taxi':11.0,'car':11.0}
ROAD_COHORT_WEIGHT=5
MAX_ROAD_COHORTS=1200
ROAD_INDEX={road['id']:road for road in ROADS}


def _edge_key(a,b):
    return tuple(sorted(((round(a[0],6),round(a[1],6)),(round(b[0],6),round(b[1],6)))))


ROAD_EDGE_INDEX={_edge_key(r['geometry']['coordinates'][0],r['geometry']['coordinates'][-1]):r['id'] for r in ROADS}
HOME_PLACES=tuple(p['id'] for p in PLACES if p['kind']=='residential')
WORK_PLACES=('central_station','convention_centre','west_foundry','innovation_park','northstar_regional_hospital',
             'northstar_airport','northstar_harbour','university')
# School campuses are authored demand anchors at existing community hubs, not
# additions to the authoritative facility atlas or actual school addresses.
SCHOOL_PLACES=('university','north_centre','south_exchange','science_centre','harbour_market')
SOCIAL_PLACES=('old_town','civic_square','riverfront','stadium','central_hotels','harbour_market','sports_park')
COMMUTE_PAIRS={
    'work':tuple((home,WORK_PLACES[(index*3+offset)%len(WORK_PLACES)]) for index,home in enumerate(HOME_PLACES) for offset in (0,1)),
    'school':tuple((home,min(SCHOOL_PLACES,key=lambda p:math.hypot(PLACE_INDEX[home]['x']-PLACE_INDEX[p]['x'],PLACE_INDEX[home]['y']-PLACE_INDEX[p]['y']))) for home in HOME_PLACES),
    'social':tuple((home,SOCIAL_PLACES[(index+offset)%len(SOCIAL_PLACES)]) for index,home in enumerate(HOME_PLACES) for offset in (0,3)),
}


def _random(state):
    # Numerical state survives an ordinary JSON round trip without pickle.
    value=state['rng_state']
    value^=(value<<13)&0xffffffff;value^=value>>17;value^=(value<<5)&0xffffffff
    state['rng_state']=value&0xffffffff
    return state['rng_state']/4294967296


def _choice(state,items):return items[min(len(items)-1,int(_random(state)*len(items)))]
def _iso(seconds):return datetime.fromtimestamp(seconds,TZ).isoformat(timespec='seconds')


def _measure(points):
    cumulative=[0.]
    for a,b in zip(points,points[1:]):cumulative.append(cumulative[-1]+math.dist(a,b))
    return cumulative


ROUTE_MEASURES={r['id']:_measure(r['points']) for r in ROUTES}


def _point(points,cumulative,distance):
    if distance<=0:return points[0][0],points[0][1]
    if distance>=cumulative[-1]:return points[-1][0],points[-1][1]
    index=bisect.bisect_right(cumulative,distance)-1
    a,b=points[index],points[index+1]
    length=cumulative[index+1]-cumulative[index]
    ratio=(distance-cumulative[index])/length if length else 0.
    return a[0]+(b[0]-a[0])*ratio,a[1]+(b[1]-a[1])*ratio


@lru_cache(maxsize=8192)
def _path(origin,destination):
    route=road_path(origin,destination)
    return route['points'],_measure(route['points'])


@lru_cache(maxsize=1024)
def _traffic_path(origin,destination):
    points,measure=_path(origin,destination)
    roads=[ROAD_EDGE_INDEX[_edge_key(a,b)] for a,b in zip(points,points[1:])]
    return points,measure,roads


def _school_term(local):
    """Authored recurring term, not a real school-board calendar."""
    return local.weekday()<5 and local.month not in (7,8) and not (
        local.month==12 and local.day>=20 or local.month==1 and local.day<=4)


def commute_profile(timestamp):
    """Authored hourly departures; traffic layer is separate from traveler dots."""
    now=datetime.fromtimestamp(timestamp,TZ);hour=now.hour+now.minute/60+now.second/3600
    peak=lambda centre,width:math.exp(-.5*((hour-centre)/width)**2)
    weekday=now.weekday()<5;school=_school_term(now)
    work_scale=1. if weekday else .20
    rates={'work_out':work_scale*(12500*peak(7.8,1.25)+1000*peak(12.5,1.0)),
           'work_home':work_scale*13500*peak(17.,1.45),
           'school_out':4800*peak(8.05,.65) if school else 0.,
           'school_home':5600*peak(15.1,.9) if school else 0.,
           'social':850+(7800 if weekday else 10000)*peak(14.0,4.)+2600*peak(20.,1.8)}
    total=sum(rates.values())
    leading=max(rates,key=rates.get)
    period={'work_out':'Morning work commute','work_home':'Evening journey home',
            'school_out':'School arrival peak','school_home':'School dismissal',
            'social':'Daytime / social travel' if 7<=hour<23 else 'Overnight travel'}[leading]
    if weekday and 6.5<=hour<9.5:period='Work & school arrival' if school else 'Morning work commute'
    elif school and 14<=hour<16:period='School dismissal'
    elif weekday and 16<=hour<19:period='Evening journey home'
    return {'weekday':weekday,'school_in_session':school,'commute_period':period,
            'vehicles_per_hour':round(total,1),'demand_factor':round(total/10000,3),'rates':rates,
            'work_factor':round((rates['work_out']+rates['work_home'])/13500,3),
            'school_factor':round((rates['school_out']+rates['school_home'])/5600,3)}


def _road_random(traffic):
    value=traffic['rng_state'];value^=(value<<13)&0xffffffff;value^=value>>17;value^=(value<<5)&0xffffffff
    traffic['rng_state']=value&0xffffffff
    return traffic['rng_state']/4294967296


def _road_choice(traffic,items):
    return items[min(len(items)-1,int(_road_random(traffic)*len(items)))]


def _traffic_od(traffic,profile):
    selection=_road_random(traffic)*sum(profile['rates'].values());chosen='social'
    for key,rate in profile['rates'].items():
        selection-=rate
        if selection<=0:chosen=key;break
    purpose=chosen.split('_')[0]
    home,activity=_road_choice(traffic,COMMUTE_PAIRS[purpose])
    homebound=chosen.endswith('_home') or (chosen=='social' and _road_random(traffic)<.5)
    return (activity,home) if homebound else (home,activity),purpose,'homebound' if homebound else 'outbound'


def _road_position(vehicle):
    points,measure,roads=_traffic_path(vehicle['origin_id'],vehicle['destination_id'])
    position=min(max(vehicle['distance'],0.),measure[-1])
    index=min(len(roads)-1,max(0,bisect.bisect_right(measure,position)-1))
    vehicle['x'],vehicle['y']=_point(points,measure,position)
    a,b=points[index],points[index+1]
    vehicle['heading']=math.degrees(math.atan2(b[1]-a[1],b[0]-a[0]))
    vehicle['road_id']=roads[index]
    return measure[-1]


def _spawn_road_vehicle(state,profile,warm=False):
    traffic=state['road_traffic'];traffic['requested_vehicle_journeys']+=ROAD_COHORT_WEIGHT
    if len(traffic['vehicles'])>=MAX_ROAD_COHORTS:
        traffic['suppressed_vehicle_journeys']+=ROAD_COHORT_WEIGHT
        return
    (origin,destination),purpose,direction=_traffic_od(traffic,profile)
    if not warm and not weather_response.allow(state,origin,destination,purpose,direction,ROAD_COHORT_WEIGHT,road=True):
        traffic['suppressed_vehicle_journeys']+=ROAD_COHORT_WEIGHT
        return
    points,measure,roads=_traffic_path(origin,destination)
    traffic['next_id']+=1
    draw=_road_random(traffic)
    mode='bus' if purpose=='school' and draw<.045 else ('taxi' if draw>.9 else 'car')
    occupancy=20 if mode=='bus' else (1.3 if mode=='taxi' else 1.35)
    vehicle={'id':f'road-{traffic["next_id"]:07d}','origin_id':origin,'destination_id':destination,
             'mode':mode,'purpose':purpose,'direction':direction,'vehicle_weight':ROAD_COHORT_WEIGHT,
             'represented_vehicles':ROAD_COHORT_WEIGHT,'represented_people':round(ROAD_COHORT_WEIGHT*occupancy,2),
             'distance':measure[-1]*_road_random(traffic) if warm else 0.,'created_at':state['t'],
             'status':'moving','speed_kph':0.,'warm_start':warm}
    _road_position(vehicle)
    traffic['vehicles'].append(vehicle)
    traffic['generated_vehicle_journeys']+=ROAD_COHORT_WEIGHT


def _update_road_loads(state):
    traffic=state['road_traffic'];profile=commute_profile(state['t']);loads={}
    # Assign OD departure flow to its physical shortest path, retaining road
    # capacities. This is an authored demand estimate, not measured traffic.
    for purpose in ('work','school','social'):
        rate=profile['rates'].get(purpose+'_out',0)+profile['rates'].get(purpose+'_home',0) if purpose!='social' else profile['rates']['social']
        if rate<=.01:continue
        pairs=COMMUTE_PAIRS[purpose]
        for home,activity in pairs:
            for road_id in _traffic_path(home,activity)[2]:
                p=weather_response.policy(state,home,activity)
                fraction=p['school_attendance_factor'] if purpose=='school' else .85*p['work_attendance_factor']+.15*p['essential_staff_factor'] if purpose=='work' else p['nonessential_travel_factor']
                loads[road_id]=loads.get(road_id,0)+rate/len(pairs)*fraction
    roads={}
    incidents=[i for i in state['incidents'] if i['status'] not in ('resolved','closed')]
    for ident,flow in loads.items():
        road=ROAD_INDEX[ident];capacity={'arterial':4800,'local':1600,'access':1200}.get(road['class'],1800)
        points=road['geometry']['coordinates'];mid=((points[0][0]+points[-1][0])/2,(points[0][1]+points[-1][1])/2)
        for incident in incidents:
            if incident['type']=='storm':capacity*=.72
            elif incident['type'] in ('collision','signal_failure') and math.hypot(mid[0]-incident['x'],mid[1]-incident['y'])<600:
                capacity*=.35 if incident['type']=='collision' else .6
        ratio=flow/max(100,capacity)
        speed_factor=max(.08,1/(1+.15*ratio**4))
        roads[ident]={'id':ident,'flow_vehicles_hour':round(flow,1),'capacity_vehicles_hour':round(capacity,1),
                      'congestion_pct':round((1-speed_factor)*100,1),'speed_kph':round(road['speed_kph']*speed_factor,1)}
    traffic['roads']=roads;traffic['profile']=profile;traffic['last_load_update']=state['t']


def ensure_road_traffic(state):
    """Add the traffic model to old saved states without changing traveler IDs."""
    if 'road_traffic' in state:return state['road_traffic']
    traffic={'version':'northstar-road-demand-v1','rng_state':((state['seed']^0xA83C17)&0xffffffff) or 1,
             'vehicles':[],'roads':{},'next_id':0,'carry':0.,'last_load_update':state['t']-60,
             'generated_vehicle_journeys':0,'completed_vehicle_journeys':0,'requested_vehicle_journeys':0,
             'suppressed_vehicle_journeys':0,'individual_work_trips':0,'individual_school_trips':0,
             'commute_carry':{},'history':[],'last_history_t':state['t']-60,'initialized_at':_iso(state['t'])}
    state['road_traffic']=traffic
    profile=commute_profile(state['t'])
    # Warm-start a 15-minute in-flight demand window; these are explicitly
    # initialized cohorts rather than extra individual dots or a census.
    initial=min(1000,max(30,round(profile['vehicles_per_hour']*.25/ROAD_COHORT_WEIGHT)))
    for _ in range(initial):_spawn_road_vehicle(state,profile,warm=True)
    _update_road_loads(state)
    return traffic


def _advance_road_traffic(state,dt):
    traffic=state['road_traffic']
    if state['t']-traffic['last_load_update']>=60:_update_road_loads(state)
    profile=commute_profile(state['t'])
    traffic['carry']+=profile['vehicles_per_hour']/3600/ROAD_COHORT_WEIGHT*dt*min(1,environment_effects.multiplier(state, 'demand'))
    while traffic['carry']>=1:
        traffic['carry']-=1;_spawn_road_vehicle(state,profile)
    active=[]
    for vehicle in traffic['vehicles']:
        remaining=dt
        points,measure,roads=_traffic_path(vehicle['origin_id'],vehicle['destination_id'])
        # Spend time on each crossed road at that road's own congestion speed.
        while remaining>1e-8 and vehicle['distance']<measure[-1]-1e-8:
            index=min(len(roads)-1,max(0,bisect.bisect_right(measure,vehicle['distance']+1e-8)-1))
            road_id=roads[index];road=ROAD_INDEX[road_id]
            condition=traffic['roads'].get(road_id,{})
            speed=max(2.,condition.get('speed_kph',road['speed_kph']))*(.85 if vehicle['mode']=='bus' else 1.)/environment_effects.multiplier(state, 'travel_time')
            distance=measure[index+1]-vehicle['distance'];seconds=distance/(speed/3.6)
            vehicle['distance']+=min(remaining,seconds)*speed/3.6
            remaining-=min(remaining,seconds)
            vehicle['speed_kph']=round(speed,1)
            vehicle['status']='slow' if speed<road['speed_kph']*.55 else 'moving'
        _road_position(vehicle)
        if vehicle['distance']>=measure[-1]-1e-8:traffic['completed_vehicle_journeys']+=vehicle['vehicle_weight']
        else:active.append(vehicle)
    traffic['vehicles']=active
    if state['t']-traffic['last_history_t']>=60:
        traffic['last_history_t']=state['t']
        values=[traffic['roads'].get(v['road_id'],{}).get('congestion_pct',0) for v in active]
        traffic['history'].append({'time':_iso(state['t']),'represented_vehicles':len(active)*ROAD_COHORT_WEIGHT,
            'congestion_pct':round(sum(values)/max(1,len(values)),1),'vehicles_per_hour':profile['vehicles_per_hour']})
        traffic['history']=traffic['history'][-180:]


def _commute_person(state,home,activity,purpose,homebound):
    origin,destination=(activity,home) if homebound else (home,activity)
    if not weather_response.allow(state,origin,destination,purpose,'homebound' if homebound else 'outbound'):
        state['demand_requested']+=1;state['suppressed_demand']+=1
        return None
    start,end=PLACE_INDEX[origin],PLACE_INDEX[destination]
    person=None;attempted=False
    if _random(state)<.65:
        candidates=[]
        for route in ROUTES:
            if not _in_service(route,state['t']):continue
            first=min(range(len(route['stops'])),key=lambda i:math.hypot(PLACE_INDEX[route['stops'][i]]['x']-start['x'],PLACE_INDEX[route['stops'][i]]['y']-start['y']))
            last=min(range(len(route['stops'])),key=lambda i:math.hypot(PLACE_INDEX[route['stops'][i]]['x']-end['x'],PLACE_INDEX[route['stops'][i]]['y']-end['y']))
            access=math.dist((start['x'],start['y']),(PLACE_INDEX[route['stops'][first]]['x'],PLACE_INDEX[route['stops'][first]]['y']))
            egress=math.dist((end['x'],end['y']),(PLACE_INDEX[route['stops'][last]]['x'],PLACE_INDEX[route['stops'][last]]['y']))
            if first!=last and access<1800 and egress<1800:
                candidates.append((access+egress+abs(route['stop_distances'][last]-route['stop_distances'][first])*.1,route,first,last))
        if candidates:
            attempted=True
            _,route,first,last=min(candidates,key=lambda c:c[0]);stop=route['stops'][first]
            if origin==stop:
                person=_new_person(state,stop,route['stops'][last],route['mode'],route['id'],1 if last>first else -1)
            else:
                person=_direct_trip(state,origin,stop,'walk')
                if person:person['pending_transit']={'route_id':route['id'],'origin_id':stop,'destination_id':route['stops'][last],
                                                    'direction':1 if last>first else -1}
            if person and destination!=route['stops'][last]:person['final_destination_id']=destination
    if person is None and not attempted:person=_direct_trip(state,origin,destination)
    if person:
        person.update(trip_purpose=purpose,home_id=home,activity_place_id=activity,commute_direction='homebound' if homebound else 'outbound')
        key='individual_'+purpose+'_trips';state['road_traffic'][key]+=1
    return person


def _commute_demand(state,dt):
    profile=commute_profile(state['t']);traffic=state['road_traffic']
    # Explicit sample releases, independent of the weighted road model: 10% of
    # the authored work/school departure rate; never a population expansion.
    for name in ('work_out','work_home','school_out','school_home'):
        traffic['commute_carry'][name]=traffic['commute_carry'].get(name,0)+profile['rates'][name]*.10/3600*dt*min(1,environment_effects.multiplier(state, 'demand'))
        while traffic['commute_carry'][name]>=1:
            traffic['commute_carry'][name]-=1
            purpose=name.split('_')[0];home,activity=_choice(state,COMMUTE_PAIRS[purpose])
            _commute_person(state,home,activity,purpose,name.endswith('_home'))


def road_traffic_snapshot(state):
    """Read-only view, including for a historical state not yet migrated."""
    traffic=state.get('road_traffic',{});vehicles=traffic.get('vehicles',[])
    profile=commute_profile(state['t']);local=datetime.fromtimestamp(state['t'],TZ)
    hourly=[]
    for hour in range(24):
        point=commute_profile(local.replace(hour=hour,minute=0,second=0,microsecond=0).timestamp())
        hourly.append({'hour':hour,**{key:point[key] for key in ('vehicles_per_hour','work_factor','school_factor')}})
    summary={'visible_cohorts':len(vehicles),'represented_vehicles':sum(v['vehicle_weight'] for v in vehicles),
             'represented_people':round(sum(v['represented_people'] for v in vehicles)),
             'congestion_pct':round(sum(traffic.get('roads',{}).get(v['road_id'],{}).get('congestion_pct',0) for v in vehicles)/max(1,len(vehicles)),1),
             'active_individual_travelers':len(state['people']),'work_trips':traffic.get('individual_work_trips',0),
             'school_trips':traffic.get('individual_school_trips',0),
             **{key:profile[key] for key in ('commute_period','school_in_session','weekday','demand_factor','vehicles_per_hour')},
             **{key:traffic.get(key,0) for key in ('generated_vehicle_journeys','completed_vehicle_journeys','suppressed_vehicle_journeys')},
             'vehicle_weight':ROAD_COHORT_WEIGHT,'max_visible_cohorts':MAX_ROAD_COHORTS,
             'scheduled_transit_buses':sum(v['mode']=='bus' for v in state['vehicles'])}
    return {'synthetic':True,'model_version':'northstar-road-demand-v1','initialized':bool(traffic),
            'initialized_at':traffic.get('initialized_at'),'warm_start_window_minutes':15,'summary':summary,
            'vehicles':[{key:v[key] for key in ('id','x','y','heading','mode','status','speed_kph','vehicle_weight',
                        'represented_vehicles','represented_people','road_id','purpose','direction','origin_id','destination_id')} for v in vehicles],
            'roads':list(traffic.get('roads',{}).values()),'profile':hourly,'history':list(traffic.get('history',[])),
            'school_anchors':[{'place_id':ident,'name':PLACE_INDEX[ident]['name'],'x':PLACE_INDEX[ident]['x'],'y':PLACE_INDEX[ident]['y']} for ident in SCHOOL_PLACES],
            'assumptions':['Road marks are cohorts of five modeled vehicles; traveler circles remain individual synthetic people.',
                'Road passengers are aggregate occupancy estimates and overlap the travel market; do not add them to individual dot counts.',
                'School term excludes weekends, July/August and December 20–January 4; this is an authored recurring calendar.',
                'Departures, capacities, occupancy and speed response are authored; flows are model assignments, not measured traffic.',
                'Work/school trip counters refer to generated individual sample journeys; road journey counters use vehicle weights.']}


def _vehicle_point(vehicle):
    route=ROUTE_INDEX[vehicle['route_id']]
    return _point(route['points'],ROUTE_MEASURES[route['id']],vehicle['progress'])


def _event_instances(timestamp,include_approaching=True):
    now=datetime.fromtimestamp(timestamp,TZ)
    result=[]
    # Include last year's New Year's Eve through midnight on January 1.
    for year in (now.year-1,now.year,now.year+1):
        for event in EVENTS:
            start=datetime(year,event['month'],event['day'],event['start_hour'],tzinfo=TZ)
            end=start+timedelta(hours=event['duration_hours'])
            lead=timedelta(minutes=event['demand_window_minutes']) if include_approaching else timedelta()
            if start-lead<=now<end+timedelta(hours=1):
                result.append({**event,'instance_id':f'{year}:{event["id"]}',
                               'start':start.isoformat(),'end':end.isoformat(),
                               'phase':'approaching' if now<start else ('active' if now<end else 'departing')})
    return result


def _new_person(state,origin,destination,mode,route_id=None,direction=None,event_id=None):
    if origin==destination:return None
    state['demand_requested']+=1
    if len(state['people'])>=state['max_people']:
        state['suppressed_demand']+=1
        return None
    state['next_person']+=1
    p={'id':f'p{state["next_person"]:07d}','origin_id':origin,'destination_id':destination,
       'mode':mode,'status':'queued' if route_id else 'moving','created_at':state['t'],
       'distance':0.,'route_id':route_id,'direction':direction,'vehicle_id':None,
       'wait_seconds':0.,'event_id':event_id}
    place=PLACE_INDEX[origin];p['x']=place['x'];p['y']=place['y']
    state['people'].append(p)
    state['generated_people']+=1
    if event_id:state['event_generated'][event_id]=state['event_generated'].get(event_id,0)+1
    return p


def _in_service(route,timestamp):
    now=datetime.fromtimestamp(timestamp,TZ)
    hour=now.hour+now.minute/60
    start,end=route['service_start_hour'],route['service_end_hour']
    return start<=hour<min(end,24) or (end>24 and hour<end-24)


def _transit_trip(state,source=None,destination=None,event_id=None,weather_purpose=None):
    candidates=[r for r in ROUTES if _in_service(r,state['t']) and (source is None or source in r['stops'])]
    if destination:
        target=PLACE_INDEX[destination]
        candidates.sort(key=lambda r:min(math.hypot(PLACE_INDEX[s]['x']-target['x'],PLACE_INDEX[s]['y']-target['y']) for s in r['stops']))
        candidates=candidates[:3]
    if not candidates:return _direct_trip(state,source,destination,'taxi',event_id,weather_purpose)
    route=_choice(state,candidates)
    stops=route['stops']
    if source:
        origin_index=stops.index(source)
    else:origin_index=int(_random(state)*len(stops))
    choices=[i for i in range(len(stops)) if i!=origin_index]
    if destination:
        target=PLACE_INDEX[destination]
        dest_index=min(choices,key=lambda i:math.hypot(PLACE_INDEX[stops[i]]['x']-target['x'],PLACE_INDEX[stops[i]]['y']-target['y']))
    else:dest_index=_choice(state,choices)
    if weather_purpose and not weather_response.allow(state,stops[origin_index],destination or stops[dest_index],weather_purpose):
        state['demand_requested']+=1;state['suppressed_demand']+=1
        return None
    p=_new_person(state,stops[origin_index],stops[dest_index],route['mode'],route['id'],1 if dest_index>origin_index else -1,event_id)
    if p and destination and destination!=p['destination_id']:p['final_destination_id']=destination
    return p


def _direct_trip(state,source=None,destination=None,mode=None,event_id=None,weather_purpose=None):
    if source is None:source=_choice(state,PLACES)['id']
    start=PLACE_INDEX[source]
    if destination is None:
        near=[p['id'] for p in PLACES if p['id']!=source and math.hypot(start['x']-p['x'],start['y']-p['y'])<2700]
        destination=_choice(state,near or [p['id'] for p in PLACES if p['id']!=source])
    if source==destination:return None
    if weather_purpose and not weather_response.allow(state,source,destination,weather_purpose):
        state['demand_requested']+=1;state['suppressed_demand']+=1
        return None
    path,measure=_path(source,destination)
    if mode is None:
        u=_random(state)
        mode='walk' if u<.62 and measure[-1]<3500 else ('cycle' if u<.82 and measure[-1]<8000 else 'taxi')
    if mode=='walk' and measure[-1]>5000:mode='taxi'
    return _new_person(state,source,destination,mode,event_id=event_id)


def _arrival_trip(state,arrival):
    source=arrival['place_id']
    events=[event for event in _event_instances(state['t']) if event['phase']!='departing']
    destination=_choice(state,events)['venue_id'] if events and _random(state)<.40 else _choice(state,['central_station','central_hotels','old_town','university','convention_centre'])
    if _random(state)<.83:return _transit_trip(state,source,destination)
    return _direct_trip(state,source,destination,'taxi')


def _ensure_arrivals(state):
    now=datetime.fromtimestamp(state['t'],TZ)
    known=set(state['schedule_days'])
    for offset in (-1,0,1):
        day=(now+timedelta(days=offset)).date();day_key=day.isoformat()
        if day_key in known:continue
        state['schedule_days'].append(day_key)
        def add(code,name,mode,hour,minute,passengers,sample,place,duration):
            when=datetime(day.year,day.month,day.day,hour,minute,tzinfo=TZ).timestamp()
            if when+duration<state['t']-3600:return
            before_start=min(sample,max(0,int((state['t']-when)/duration*sample)))
            state['arrivals'].append({'id':f'{day_key}:{code}','name':name,'mode':mode,'time':_iso(when),
              'timestamp':when,'passengers':passengers,'modeled_passengers':sample,'generated_people':0,
              'suppressed_people':0,'processed_people':before_start,'before_start_people':before_start,
              'place_id':place,'duration_seconds':duration,
              'status':'completed' if before_start==sample else ('disembarking' if before_start else 'scheduled'),'demand_note':'Scenario manifest; only modeled_passengers enter this bounded sample.'})
        for index,(hour,minute,origin) in enumerate([(8,0,'Halifax'),(9,20,'Toronto'),(12,10,'Montréal'),(14,40,'Ottawa'),(17,20,'Boston'),(20,15,'Toronto')]):
            add(f'air{index}',f'NS {210+index} · {origin}','aircraft',hour,minute,156,125,'northstar_airport',900)
        for index,hour in enumerate([8,10,13,16,19,22]):
            add(f'rail{index}',f'Atlantic Regional {70+index}','rail',hour,4,250,180,'northstar_rail',480)
        if 6<=day.month<=9:
            # Three or four weekly calls, with a second Saturday call in peak season.
            if day.weekday() in (1,3,5,6):
                add('cruise0','MV Aurora Atlantic','cruise',8,2,2100,600,'northstar_harbour',2400)
            if day.weekday()==5 and day.month in (7,8):
                add('cruise1','MV Northern Star','cruise',10,30,1650,420,'northstar_harbour',2100)
    state['arrivals'].sort(key=lambda a:a['timestamp'])
    state['arrivals']=[a for a in state['arrivals'] if a['timestamp']>=state['t']-86400]
    state['schedule_days']=sorted(set(state['schedule_days']))[-5:]


def create_state(seed=42,start=DEFAULT_START):
    if isinstance(seed,bool) or not isinstance(seed,int):raise ValueError('seed must be an integer')
    parsed=datetime.fromisoformat(start)
    if parsed.tzinfo is None:raise ValueError('start must contain an explicit UTC offset')
    timestamp=parsed.timestamp()
    state={'schema_version':1,'run_id':uuid4().hex[:16],'seed':seed,'rng_state':(seed&0xffffffff) or 1,
           't':timestamp,'started_at':_iso(timestamp),'paused':False,'speed':60,'max_people':MAX_PEOPLE,
           'people':[],'vehicles':[],'arrivals':[],'incidents':[],'history':[],
           'next_person':0,'next_incident':0,'generated_people':0,'completed_trips':0,
           'completed_by_mode':{},'demand_requested':0,'suppressed_demand':0,
           'total_wait_seconds':0.,'boardings':0,'background_carry':0.,'event_carry':{},'event_generated':{},
           'event_departures':{},'schedule_days':[],'last_history_t':timestamp-60,'last_schedule_day':'',
           'incident_hazard':0.,'incident_threshold':-math.log(.5),'feedback_count':0,
           'congestion_pct':0.,'last_feedback':None}
    for route in ROUTES:
        cycle_seconds=2*route['length_m']/route['speed_mps']+len(route['stops'])*50
        count=max(2,min(20,math.ceil(cycle_seconds/(route['headway_minutes']*60))))
        for i in range(count):
            phase=i/count*2*route['length_m']
            direction=1 if phase<route['length_m'] else -1
            progress=phase if direction==1 else 2*route['length_m']-phase
            distances=route['stop_distances']
            stop=bisect.bisect_right(distances,progress) if direction==1 else bisect.bisect_left(distances,progress)-1
            stop=max(0,min(len(distances)-1,stop))
            x,y=_point(route['points'],ROUTE_MEASURES[route['id']],progress)
            state['vehicles'].append({'id':f'{route["id"]}:{i+1:02d}','route_id':route['id'],'mode':route['mode'],
                'direction':direction,'progress':progress,'next_stop':stop,'hold_until':timestamp+_random(state)*15,
                'x':x,'y':y,'occupancy':0,'capacity':route['capacity'],'status':'moving','boarded':0})
    # Seed a steady-state sample: real queued, riding and direct-travel persons.
    for index in range(2400):
        if index<1000:
            p=_direct_trip(state)
            if p:
                points,measure=_path(p['origin_id'],p['destination_id'])
                p['distance']=measure[-1]*_random(state)
                p['x'],p['y']=_point(points,measure,p['distance'])
        elif index<1750:_transit_trip(state)
        else:
            vehicle=_choice(state,state['vehicles']);route=ROUTE_INDEX[vehicle['route_id']]
            if vehicle['occupancy']>=vehicle['capacity']:continue
            next_stop=vehicle['next_stop'];direction=vehicle['direction']
            candidates=list(range(next_stop,len(route['stops']))) if direction==1 else list(range(0,next_stop+1))
            dest=_choice(state,candidates)
            origin=max(0,next_stop-1) if direction==1 else min(len(route['stops'])-1,next_stop+1)
            if origin==dest:continue
            p=_new_person(state,route['stops'][origin],route['stops'][dest],route['mode'],route['id'],direction)
            if p:
                p.update(status='onboard',vehicle_id=vehicle['id'],x=vehicle['x'],y=vehicle['y'])
                vehicle['occupancy']+=1
    _ensure_arrivals(state)
    ensure_road_traffic(state)
    ensure_infrastructure(state)
    cruise_terminal.ensure_cruise_terminal(state)
    _record_history(state)
    return state


def _congestion(state):
    taxis=sum(p['mode'] in ('taxi','car') for p in state['people'])
    queued=sum(p['status']=='queued' for p in state['people'])
    incident_load=sum(12 for i in state['incidents'] if i['status'] not in ('resolved','closed'))
    traffic=state.get('road_traffic',{});roads=traffic.get('roads',{})
    values=[roads.get(v['road_id'],{}).get('congestion_pct',0) for v in traffic.get('vehicles',[])]
    road_pressure=sum(values)/max(1,len(values))
    return min(95.,8+taxis*.025+queued*.006+road_pressure*.65+incident_load)


def _route_factor(state,vehicle):
    if not _in_service(ROUTE_INDEX[vehicle['route_id']],state['t']):return 0.
    factor=1.0
    route=ROUTE_INDEX[vehicle['route_id']]
    if vehicle['mode']=='bus':
        index=min(len(route['points'])-2,max(0,bisect.bisect_right(ROUTE_MEASURES[route['id']],vehicle['progress'])-1))
        ident=ROAD_EDGE_INDEX.get(_edge_key(route['points'][index],route['points'][index+1]))
        condition=state.get('road_traffic',{}).get('roads',{}).get(ident,{})
        factor*=max(.08,1-condition.get('congestion_pct',state['congestion_pct']*.5)/100)
    factor*=transport_service_factor(state,vehicle['route_id'])
    p=weather_response.policy(state,route['stops'][0],route['stops'][-1])
    terminal=vehicle['progress']<1 or vehicle['progress']>=ROUTE_MEASURES[route['id']][-1]-1
    vehicle['weather_service_hold']=bool(terminal and not vehicle.get('occupancy') and not weather_response.attends(vehicle['id'],p['public_transit_service_factor']))
    if vehicle['weather_service_hold']:return 0.
    for incident in state['incidents']:
        if incident['status'] in ('resolved','closed'):continue
        if incident['type']=='storm' and vehicle['mode'] in ('bus','ferry'):factor*=.45
        elif incident.get('route_id')==route['id']:
            factor*=0. if incident['type']=='signal_failure' else (.32 if incident['type']=='collision' else .65)
    return factor


def _finish(state,person):
    if person.get('pending_transit'):
        leg=person.pop('pending_transit');place=PLACE_INDEX[leg['origin_id']]
        person.update(**leg,mode=ROUTE_INDEX[leg['route_id']]['mode'],status='queued',vehicle_id=None,
                      distance=0.,x=place['x'],y=place['y'],queue_entered_at=state['t'])
        return
    if person.get('office_worker_id') and not commutes.finish_leg(state,person):return
    person['status']='completed'
    state['completed_trips']+=1
    mode=person['mode'];state['completed_by_mode'][mode]=state['completed_by_mode'].get(mode,0)+1


def _service_stop(state,vehicle,queues,onboard):
    route=ROUTE_INDEX[vehicle['route_id']];stop=vehicle['next_stop'];place_id=route['stops'][stop]
    x,y=PLACE_INDEX[place_id]['x'],PLACE_INDEX[place_id]['y']
    for person in list(onboard.get(vehicle['id'],[])):
        if person['destination_id']==place_id:
            vehicle['occupancy']-=1
            if person.get('final_destination_id') and person['final_destination_id']!=place_id:
                person.update(origin_id=place_id,destination_id=person.pop('final_destination_id'),mode='walk',
                              status='moving',route_id=None,vehicle_id=None,direction=None,distance=0.,x=x,y=y)
            else:_finish(state,person)
    if stop==len(route['stops'])-1:vehicle['direction']=-1
    elif stop==0:vehicle['direction']=1
    queue=queues.get((route['id'],place_id,vehicle['direction']),[])
    for person in queue:
        if person['status']!='queued':continue
        if vehicle['occupancy']>=vehicle['capacity']:break
        person.update(status='onboard',vehicle_id=vehicle['id'],x=x,y=y)
        wait=max(0.,state['t']-person.get('queue_entered_at',person['created_at']))
        person['wait_seconds']=wait;state['total_wait_seconds']+=wait;state['boardings']+=1
        vehicle['occupancy']+=1;vehicle['boarded']+=1
        onboard.setdefault(vehicle['id'],[]).append(person)
    vehicle['next_stop']=stop+vehicle['direction']
    vehicle['hold_until']=state['t']+(30 if route['mode'] in ('ferry','rail') else 20)
    vehicle['status']='boarding'


def _move(state,dt):
    queues={};onboard={}
    for p in state['people']:
        if p['status']=='queued':queues.setdefault((p['route_id'],p['origin_id'],p['direction']),[]).append(p)
        elif p['status']=='onboard':onboard.setdefault(p['vehicle_id'],[]).append(p)
    vehicle_lookup={v['id']:v for v in state['vehicles']}
    for vehicle in state['vehicles']:
        factor=_route_factor(state,vehicle)
        if factor<=0:
            vehicle['status']='disrupted' if _in_service(ROUTE_INDEX[vehicle['route_id']],state['t']) else 'out_of_service';continue
        if vehicle['hold_until']>state['t']:
            vehicle['status']='boarding';continue
        route=ROUTE_INDEX[vehicle['route_id']]
        target=route['stop_distances'][vehicle['next_stop']]
        travel=dt*route['speed_mps']*factor/environment_effects.multiplier(state, 'travel_time')
        if abs(target-vehicle['progress'])<=travel:
            vehicle['progress']=target
            vehicle['x'],vehicle['y']=_vehicle_point(vehicle)
            _service_stop(state,vehicle,queues,onboard)
        else:
            vehicle['progress']+=travel*vehicle['direction']
            vehicle['x'],vehicle['y']=_vehicle_point(vehicle)
            vehicle['status']='delayed' if factor<.5 else 'moving'
    for p in state['people']:
        if p['status']=='onboard':
            vehicle=vehicle_lookup[p['vehicle_id']];p['x']=vehicle['x'];p['y']=vehicle['y']
        elif p['status']=='moving':
            points,measure=commutes.current_path(p) if p.get('office_worker_id') else _path(p['origin_id'],p['destination_id'])
            factor=1-state['congestion_pct']*.006 if p['mode'] in ('taxi','car') else 1.
            if p['mode'] in ('taxi','car') and len(points)>1:
                index=min(len(points)-2,max(0,bisect.bisect_right(measure,p['distance'])-1))
                ident=ROAD_EDGE_INDEX.get(_edge_key(points[index],points[index+1]))
                condition=state.get('road_traffic',{}).get('roads',{}).get(ident)
                if condition:factor=max(.08,1-condition['congestion_pct']/100)
            for incident in state['incidents']:
                if incident['status'] in ('resolved','closed'):continue
                distance=math.hypot(p['x']-incident['x'],p['y']-incident['y'])
                if distance<850:factor*=.40 if incident['type'] in ('collision','crowd_surge') else .8
            p['distance']+=SPEEDS[p['mode']]*factor*dt/environment_effects.multiplier(state, 'travel_time')
            p['x'],p['y']=_point(points,measure,p['distance'])
            if p['distance']>=measure[-1]:_finish(state,p)
    state['people']=[p for p in state['people'] if p['status']!='completed']


def _demand(state,dt):
    now=datetime.fromtimestamp(state['t'],TZ)
    if now.date().isoformat()!=state['last_schedule_day']:
        _ensure_arrivals(state);state['last_schedule_day']=now.date().isoformat()
    # Authored sample trip releases, not a population expansion factor.
    hour=now.hour+now.minute/60
    rate=.14 if hour<5 or hour>=23 else .36
    if now.weekday()>=5:rate*=.9
    state['background_carry']+=rate*dt*min(1,environment_effects.multiplier(state, 'demand'))
    while state['background_carry']>=1:
        state['background_carry']-=1
        if _random(state)<.65:_transit_trip(state,weather_purpose='social')
        else:_direct_trip(state,weather_purpose='social')
    for arrival in state['arrivals']:
        elapsed=state['t']-arrival['timestamp']
        if elapsed<0:continue
        target=min(arrival['modeled_passengers'],int((elapsed+dt)/arrival['duration_seconds']*arrival['modeled_passengers']))
        release=max(0,target-arrival['processed_people'])
        for _ in range(release):
            p=_arrival_trip(state,arrival)
            if p:arrival['generated_people']+=1
            else:arrival['suppressed_people']+=1
        arrival['processed_people']+=release
        arrival['status']='completed' if target>=arrival['modeled_passengers'] else 'disembarking'
    for event in _event_instances(state['t']):
        start=datetime.fromisoformat(event['start']).timestamp();end=datetime.fromisoformat(event['end']).timestamp()
        key=event['instance_id']
        # 2% lead-in, 0.2%/hour while active, then 2% egress over one hour.
        departing=event['phase']=='departing'
        rate=event['attendance']*(.02/3600 if departing else (.02/7200 if state['t']<start else .002/3600))
        carry_key=key+':departing' if departing else key
        state['event_carry'][carry_key]=state['event_carry'].get(carry_key,0)+rate*dt
        while state['event_carry'][carry_key]>=1:
            state['event_carry'][carry_key]-=1
            if departing:
                person=_transit_trip(state,source=event['venue_id']) if _random(state)<.72 else _direct_trip(state,source=event['venue_id'])
                if person:
                    person['event_id']=key;person['event_phase']='departing'
                    counts=state.setdefault('event_departures',{})
                    counts[key]=counts.get(key,0)+1
            elif _random(state)<.72:_transit_trip(state,destination=event['venue_id'],event_id=key,weather_purpose='social')
            else:_direct_trip(state,destination=event['venue_id'],event_id=key,weather_purpose='social')


def _endogenous_incidents(state,dt):
    # Separate person-health exposure from moving road-vehicle exposure. Queued
    # people do not create collision hazard; office passengers on one bus do not
    # count as hundreds of separate vehicles.
    moving=[p for p in state['people'] if p['status']=='moving' and p['mode'] in ('taxi','car','cycle')]
    vehicles=[v for v in state['vehicles'] if v['mode']=='bus' and v['status'] in ('moving','delayed')]
    cars=sum(p['mode'] in ('taxi','car') for p in moving)
    cycles=sum(p['mode']=='cycle' for p in moving)
    exposure=state.setdefault('traffic_exposure',{'road_vehicle_hours':0.,'cycle_hours':0.,'collision_hazard':0.,
        'collision_threshold':-math.log(.5),'collision_times':[],'model':'Authored exposure, not calibrated collision prediction.'})
    exposure['moving_road_commuters']=sum(bool(p.get('office_worker_id')) for p in moving)+sum(
        p.get('office_worker_id') is not None and p['mode']=='bus' and p['status']=='onboard' for p in state['people'])
    exposure['moving_road_vehicles']=cars+len(vehicles)
    exposure['road_vehicle_hours']+=(cars+len(vehicles))*dt/3600
    exposure['cycle_hours']+=cycles*dt/3600
    exposure['collision_times']=[t for t in exposure['collision_times'] if state['t']-t<3600]
    increment=min(2.,(cars+len(vehicles)+cycles*.35)/2000*(1+state['congestion_pct']/100))*dt/3600
    exposure['last_collision_hazard_increment']=increment
    open_incidents=sum(i['status'] not in ('resolved','closed') for i in state['incidents'])
    if open_incidents>=3:return
    exposure['collision_hazard']+=increment
    if exposure['collision_hazard']>=exposure['collision_threshold'] and len(exposure['collision_times'])<2 and (moving or vehicles):
        target=_choice(state,moving+vehicles)
        nearest=min(PLACES,key=lambda place:math.hypot(target['x']-place['x'],target['y']-place['y']))
        roll=_random(state);casualties=3 if roll<.01 else 2 if roll<.04 else 1 if roll<.25 else 0
        incident=create_incident(state,'collision',nearest['id'],casualties,target.get('route_id'),source='road_exposure_model')
        from .catalog import district_at
        try:
            district=district_at(target['x'],target['y'])
        except ValueError:
            district=None
        if district:
            incident.update(x=target['x'],y=target['y'],district_id=district,location={
                'crs':'northstar-local-m-v1','units':'m','x':target['x'],'y':target['y'],
                'district_id':district,'precision':'simulated_point','provenance':'moving_transit_agent'})
        incident['exposure']={'moving_road_vehicles':cars+len(vehicles),'moving_road_commuters':exposure['moving_road_commuters'],
            'office_worker_id':target.get('office_worker_id'),'workplace_id':target.get('workplace_id')}
        exposure['collision_times'].append(state['t']);exposure['collision_hazard']=0.
        exposure['collision_threshold']=-math.log(max(.000001,1-_random(state)))
        return
    # Existing authored medical event process remains independent of transport mode.
    state['incident_hazard']+=dt/10800*(len(state['people'])/2500)*.82
    if state['incident_hazard']<state['incident_threshold'] or not state['people']:return
    state['incident_hazard']=0.;state['incident_threshold']=-math.log(max(.000001,1-_random(state)))
    person=_choice(state,state['people'])
    nearest=min(PLACES,key=lambda place:math.hypot(person['x']-place['x'],person['y']-place['y']))
    create_incident(state,'medical',nearest['id'],1,person.get('route_id'),source='exposure_model')


def _maintenance(state):
    for incident in state['incidents']:
        if incident['status'] in ('resolved','closed') or incident['type']!='signal_failure':continue
        if incident['casualties'] or incident.get('dispatch_status') in ('pending','sent','retry','blocked'):continue
        created=datetime.fromisoformat(incident['created_at']).timestamp()
        if state['t']-created>=1200:
            incident.update(status='resolved',resolved_at=_iso(state['t']),updated_at=_iso(state['t']),resolution_source='transit_maintenance')


def advance(state,seconds,registry=None,environment=None):
    """Advance requested simulation seconds, independent of display pause/speed."""
    if isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or seconds<0 or seconds>86400:
        raise ValueError('seconds must be a finite number between 0 and 86400')
    if seconds > 0:
        state['environment'] = environment_effects.applied('transit', environment, _iso(state['t']),
            'Weather suppresses new school, work and discretionary journeys; underway and registered return journeys finish; essential workers retain a separate roster.', ['demand', 'travel_time'])
    weather_response.begin(state)
    commutes.prepare(state,registry)
    ensure_road_traffic(state)
    ensure_infrastructure(state)
    remaining=float(seconds)
    while remaining>1e-8:
        dt=min(STEP_SECONDS,remaining)
        _maintenance(state)
        _advance_road_traffic(state,dt)
        state['congestion_pct']=_congestion(state)
        commutes.release(state,dt,registry)
        _commute_demand(state,dt)
        _demand(state,dt)
        _move(state,dt)
        cruise_terminal.advance_cruise_terminal(state,dt)
        commutes.observe_contacts(state,dt)
        advance_infrastructure(state,dt)
        state['t']+=dt
        _endogenous_incidents(state,dt)
        if state['t']-state['last_history_t']>=60:_record_history(state)
        remaining-=dt
    if seconds > 0:
        state['environment']['applied_simulated_at'] = _iso(state['t'])
        weather_response.finish(state)
    return state


def _record_history(state):
    state['last_history_t']=state['t']
    state['history'].append({'time':_iso(state['t']),'active_people':len(state['people']),
        'queued_people':sum(p['status']=='queued' for p in state['people']),
        'completed_trips':state['completed_trips'],'congestion_pct':round(state['congestion_pct'],1)})
    state['history']=state['history'][-360:]


def create_incident(state,kind,place_id,casualties=0,route_id=None,source='operator'):
    if kind not in INCIDENT_KINDS:raise ValueError('Unknown incident kind')
    if place_id not in PLACE_INDEX:raise ValueError('Unknown place_id')
    if isinstance(casualties,bool) or not isinstance(casualties,int) or not 0<=casualties<=30:
        raise ValueError('casualties must be an integer between 0 and 30')
    if route_id is not None and route_id not in ROUTE_INDEX:raise ValueError('Unknown route_id')
    place=PLACE_INDEX[place_id]
    if route_id is None:
        routes=[r for r in ROUTES if place_id in r['stops']]
        if routes:route_id=routes[0]['id']
        elif kind=='signal_failure':
            route_id=min(ROUTES,key=lambda r:min(math.hypot(place['x']-x,place['y']-y) for x,y in r['points']))['id']
    location={'crs':'northstar-local-m-v1','units':'m','x':place['x'],'y':place['y'],
              'district_id':place['district_id'],'precision':'facility' if place.get('facility_id') else 'simulated_point',
              'provenance':'shared_facility_atlas' if place.get('facility_id') else 'authored_transit_place'}
    if place.get('facility_id'):location['facility_id']=place['facility_id']
    state['next_incident']+=1
    titles={'collision':'Transport collision','medical':'Passenger medical emergency','crowd_surge':'Crowd pressure alert',
            'signal_failure':'Signal system failure','storm':'Severe weather disruption'}
    incident={'id':f'transit-{state["run_id"]}-{state["next_incident"]:06d}','type':kind,
       'title':titles[kind]+' · '+place['name'],'place_id':place_id,'district_id':place['district_id'],
       'location':location,'x':place['x'],'y':place['y'],'route_id':route_id,'source':source,
       'severity':'critical' if casualties>=3 or kind=='crowd_surge' else ('high' if casualties or kind in ('collision','storm') else 'moderate'),
       'casualties':casualties,'status':'reported','created_at':_iso(state['t']),'updated_at':_iso(state['t']),
       'requires_911':kind in ('collision','medical','crowd_surge','storm') or casualties>0,
       'feedback':[],'synthetic':True,'transport_impact':'service_stopped' if kind=='signal_failure' else 'reduced_speed'}
    state['incidents'].append(incident)
    return incident


def apply_feedback(state,report):
    """Apply ordered operational status; hospital-only clinical updates don't clear roads."""
    ident=report.get('incident_id') or report.get('source_incident_id')
    incident=next((i for i in state['incidents'] if i['id']==ident),None)
    if incident is None:raise ValueError('Unknown incident_id')
    status=report.get('status') or report.get('incident_status')
    source=str(report.get('source') or report.get('simulator') or report.get('system') or 'external')
    entry={**report,'received_at':_iso(state['t'])}
    incident['feedback'].append(entry);incident['feedback']=incident['feedback'][-30:]
    incident['updated_at']=_iso(state['t'])
    clinical=any(word in source.lower() for word in ('hospital','clinic'))
    may_close=not incident.get('requires_911') or '911' in source.lower()
    if status in STATUS_ORDER and not clinical and (status not in ('resolved','closed') or may_close):
        if STATUS_ORDER[status]>=STATUS_ORDER.get(incident['status'],0):
            incident['status']='resolved' if status=='closed' else status
            if incident['status']=='resolved':incident['resolved_at']=_iso(state['t'])
    if clinical:incident['clinical_status']=status or report.get('outcome','received')
    if report.get('external_incident_id'):incident['external_incident_id']=report['external_incident_id']
    state['feedback_count']+=1;state['last_feedback']=entry
    return incident


def _sensor_readings(state):
    local={sensor['place_id']:{'people':0,'queued':0,'traffic':0} for sensor in SENSORS}
    road_vehicles=state.get('road_traffic',{}).get('vehicles')
    for place_id,counts in local.items():
        place=PLACE_INDEX[place_id]
        for p in state['people']:
            if abs(p['x']-place['x'])<650 and abs(p['y']-place['y'])<650 and math.hypot(p['x']-place['x'],p['y']-place['y'])<650:
                counts['people']+=1
                counts['queued']+=p['status']=='queued'
                if road_vehicles is None:counts['traffic']+=p['mode'] in ('taxi','car')
        if road_vehicles is not None:
            counts['traffic']=sum(v['vehicle_weight'] for v in road_vehicles
                if abs(v['x']-place['x'])<650 and abs(v['y']-place['y'])<650 and math.hypot(v['x']-place['x'],v['y']-place['y'])<650)
    alarms={i['place_id'] for i in state['incidents'] if i['status'] not in ('resolved','closed')}
    sensors=[]
    for sensor in SENSORS:
        counts=local[sensor['place_id']];kind=sensor['kind']
        if kind=='footfall':value=counts['people'];ratio=value/250
        elif kind=='queue':value=counts['queued'];ratio=value/100
        elif kind=='air_quality':
            # Aggregate road counts are larger than the earlier sampled taxi
            # counts. Use their own authored proxy scale, not the old multiplier.
            coefficient=.035 if 'road_traffic' in state else .45
            value=round(4.+counts['traffic']*coefficient+counts['people']*.013,1);ratio=value/22
        elif kind=='noise':
            traffic_noise=6*math.log10(1+counts['traffic']/10) if 'road_traffic' in state else counts['traffic']*.07
            value=round(43+10*math.log10(1+counts['people'])+traffic_noise,1);ratio=value/72
        else:value=round(min(100,counts['traffic']*1.4+counts['queued']*.08+state['congestion_pct']*.3),1);ratio=value/65
        alarm=sensor['place_id'] in alarms
        status='critical' if alarm or ratio>=1.5 else ('warning' if ratio>=1 else 'normal')
        sensors.append({**sensor,'value':value,'status':status,'updated_at':_iso(state['t']),
                        'derived_from':'individual traveler sample and weighted road vehicles within 650 m; authored sensor response' if road_vehicles is not None else 'modeled people within 650 m; authored sensor response','synthetic':True})
    return sensors


GATEWAY_PATHS={
    'aircraft':{'approach':[[23000,5000],[19100,8500],[15500,11500]],
                'departure':[[15500,11500],[17100,10800],[23000,8000],[27800,7500]],
                'approach_seconds':600,'departure_seconds':420},
    'cruise':{'approach':[[29400,26000],[28400,24200],[27300,23000],[26500,22500]],
              'departure':[[26500,22500],[27300,23000],[28400,24200],[29400,26000]],
              'approach_seconds':900,'departure_seconds':900},
}


def _gateway_vehicles(state):
    """Schematic flight/ship movements derived from timetable, never passenger counts."""
    gateways=[]
    for arrival in state['arrivals']:
        mode=arrival['mode']
        if mode not in GATEWAY_PATHS:continue
        geometry=GATEWAY_PATHS[mode];elapsed=state['t']-arrival['timestamp']
        dwell=arrival['duration_seconds']
        if elapsed < -geometry['approach_seconds'] or elapsed >= dwell+geometry['departure_seconds']:continue
        if elapsed<0:
            phase='approaching';points=geometry['approach']
            progress=1+elapsed/geometry['approach_seconds']
        elif elapsed<dwell:
            phase='disembarking';place=PLACE_INDEX[arrival['place_id']]
            points=[[place['x'],place['y']],[place['x'],place['y']]];progress=elapsed/dwell
        else:
            phase='departing';points=geometry['departure']
            progress=(elapsed-dwell)/geometry['departure_seconds']
        measures=_measure(points);distance=measures[-1]*progress
        x,y=_point(points,measures,distance)
        index=min(len(points)-2,max(0,bisect.bisect_right(measures,distance)-1))
        a,b=points[index],points[index+1]
        heading=math.degrees(math.atan2(b[1]-a[1],b[0]-a[0])) if a!=b else 0.
        gateways.append({'id':'gateway:'+arrival['id'],'arrival_id':arrival['id'],
            'name':arrival['name'],'mode':mode,'x':x,'y':y,'heading':heading,'status':phase,
            'place_id':arrival['place_id'],'points':points,'progress':round(progress,6),
            'schematic':True,'model_note':'Authored timetable animation; not flight or marine physics.'})
    return gateways


def snapshot(state):
    now=datetime.fromtimestamp(state['t'],TZ)
    sensors=_sensor_readings(state)
    counts=Counter(p['status'] for p in state['people'])
    events=_event_instances(state['t'])
    for event in events:
        event['modeled_attendance']=state['event_generated'].get(event['instance_id'],0)
        event['modeled_departures']=state.get('event_departures',{}).get(event['instance_id'],0)
    stats={'active_people':len(state['people']),'moving_people':counts['moving'],'queued_people':counts['queued'],
           'onboard_people':counts['onboard'],'completed_trips':state['completed_trips'],
           'generated_people':state['generated_people'],'demand_requested':state['demand_requested'],
           'suppressed_demand':state['suppressed_demand'],'max_people':state['max_people'],
           'avg_wait_minutes':round(state['total_wait_seconds']/max(1,state['boardings'])/60,1),
           'current_avg_queue_minutes':round(sum(max(0,state['t']-p.get('queue_entered_at',p['created_at'])) for p in state['people'] if p['status']=='queued')/max(1,counts['queued'])/60,1),
           'congestion_pct':round(_congestion(state),1),
           'active_incidents':sum(i['status'] not in ('resolved','closed') for i in state['incidents']),
           'active_events':len(events),'cruise_season':6<=now.month<=9,
           'sensor_alerts':sum(s['status']!='normal' for s in sensors),'sensors_online':len(sensors),
           'model_scope':'Each dot is one modeled person; bounded sample, not city population.',
           'feedback_count':state['feedback_count'],'boardings':state['boardings'],
           'gateway_people_today':sum(a['generated_people'] for a in state['arrivals']
               if datetime.fromtimestamp(a['timestamp'],TZ).date()==now.date())}
    people=[]
    for p in state['people']:
        people.append({key:p.get(key) for key in ('id','x','y','mode','status','origin_id','destination_id','route_id','vehicle_id','event_id','event_phase','office_worker_id','workplace_id','business_id','commute_direction','trip_id','commute_leg','requested_mode','fallback_reason','trip_purpose','home_id','activity_place_id')})
    modes=[]
    for mode in ('subway','bus','rail','ferry','walk','cycle','taxi','car'):
        modes.append({'mode':mode,'people':sum(p['mode']==mode for p in state['people']),
                      'vehicles':sum(v['mode']==mode for v in state['vehicles']),
                      'queued':sum(p['mode']==mode and p['status']=='queued' for p in state['people']),
                      'completed':state['completed_by_mode'].get(mode,0),'color':COLORS[mode]})
    arrivals=[]
    for arrival in state['arrivals']:
        if arrival['timestamp']>=state['t']-7200 and arrival['timestamp']<state['t']+86400:
            visible={k:v for k,v in arrival.items() if k not in ('timestamp','processed_people')}
            if visible['status']=='scheduled' and arrival['timestamp']-state['t']<=300:visible['status']='arriving'
            arrivals.append(visible)
    return {'schema_version':1,'run_id':state['run_id'],'synthetic':True,'city_id':CITY['id'],
            'world_version':CITY['world_version'],'clock':{'iso':_iso(state['t']),
               'local_label':now.strftime('%a %d %b %Y · %H:%M:%S %Z'),'timezone':'America/Moncton',
               'paused':state['paused'],'speed':state['speed']},
            'stats':stats,'people':people,'vehicles':[{k:v for k,v in vehicle.items() if k not in ('hold_until','progress','next_stop')} for vehicle in state['vehicles']],
            'sensors':sensors,'arrivals':arrivals,'gateway_vehicles':_gateway_vehicles(state),'incidents':[dict(i) for i in state['incidents'][-100:]],
            'weather_response_outcomes':state.get('weather_response_outcomes',{}),'commutes':commutes.summary(state),'workplace_revision':state.get('workplace_revision',0),
            'office_workplaces':state.get('commute_workplaces',[]),'traffic_exposure':state.get('traffic_exposure',{}),
            'road_traffic':road_traffic_snapshot(state),'infrastructure':infrastructure_snapshot(state),
            'cruise_terminal':cruise_terminal.cruise_terminal_summary(state),
            'scene':cruise_terminal.cruise_terminal_scene(state),
            'history':[dict(row) for row in state['history']],'mode_stats':modes,'active_events':events,
            'model':{'name':'Northstar authored operations model','version':'1.0','sample_cap':state['max_people'],
                     'calibrated':False,'cruise_season':'June–September','time_step_seconds':STEP_SECONDS,
                     'notes':['Simulated operations, not observed city data.','One dot = one modeled person.',
                              'Scenario manifests differ from modeled passenger releases; suppressed demand is counted.',
                              'Event arrivals and egress are independent samples; registered office commuters have linked outbound/return itineraries.',
                              'Purpose-tagged background work/school journeys use recurring OD demand; weighted road cohorts are a separate aggregate layer and must not be added to individual sample totals.',
                              'Road congestion and sensor response are authored proxies, not traffic or air-quality forecasts.']}}
