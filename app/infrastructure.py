"""Activity-driven, conservative, JSON-persisted synthetic facility utilities.

No network, database, wall clock or random generator. Call advance_infrastructure
AFTER transport movement and BEFORE incrementing state['t']; it accounts for dt
seconds beginning at state['t']. Facility counters begin at installation, never
at the original scenario date when installed into an existing saved run.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime
import math
from zoneinfo import ZoneInfo

from .catalog import PLACE_INDEX, ROUTE_INDEX

TZ=ZoneInfo('America/Moncton')
VERSION='1.0'
UTILITIES=('power','elevators','internet','water','sewer')
FACILITY_IDS=list(dict.fromkeys(ROUTE_INDEX['metro_1']['stops']+[
    'northstar_harbour','north_depot','south_exchange','river_pier','marina']))
SCOPE='Authored facility utilities driven by the modeled traveler sample; not metered real-city usage or engineering forecasts. Cumulative meters begin at installation.'
# base kW, grid kW, backup kW, battery kWh, elevator assets, Mbps, water m3/h,
# tank m3, baseline water m3/h, sewer pumping m3/h, sewer buffer m3.
PROFILES={
 'airport':(480,1800,260,520,8,2500,75,100,12,65,18),
 'harbour':(200,800,110,220,4,1000,28,36,5,24,8),
 'rail':(150,1000,90,180,4,1000,20,24,3,18,6),
 'depot':(100,500,70,140,2,500,12,18,2,10,4),
 'station':(38,650,25,50,2,300,6,8,.7,5,2),
 'ferry_terminal':(28,140,20,40,2,250,5,7,.5,4,2),
}


def _iso(t):return datetime.fromtimestamp(t,TZ).isoformat(timespec='seconds')


def _number(value,name,low,high):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:
        raise ValueError(f'{name} must be a finite number from {low} to {high}')
    return float(value)


def _kind(ident):
    if ident=='north_depot':return 'depot'
    return PLACE_INDEX[ident]['kind'] if PLACE_INDEX[ident]['kind'] in PROFILES else 'station'


def _initial_facility(ident):
    place=PLACE_INDEX[ident];kind=_kind(ident)
    base,grid,backup,battery,count,mbps,inlet,tank,water_base,sewer_capacity,sewer_storage=PROFILES[kind]
    assets=[{'id':f'{ident}:lift:{i+1:02d}','name':f'Accessible lift {i+1}',
             'status':'idle','queue_people':0,'in_car_people':0,'capacity_people':12,
             'trips':0,'completed_passengers':0,'energy_kwh':0.,
             'remaining_seconds':0.,'trip_seconds':30.,'motor_kw':9.,'idle_kw':.08}
            for i in range(count)]
    return {'id':ident,'name':place['name'],'x':place['x'],'y':place['y'],'kind':kind,
      'status':'normal','activity':{'present_people':0,'entries_people':0,'exits_people':0,'vehicles_present':0,
                                  'entries_total_people':0,'exits_total_people':0},
      'power':{'demand_kw':0.,'served_kw':0.,'grid_kw':0.,'backup_kw':0.,'charging_kw':0.,'unserved_kw':0.,
               'grid_capacity_kw':float(grid),'available_grid_capacity_kw':float(grid),'backup_capacity_kw':float(backup),'backup_remaining_kwh':float(battery),
               'backup_initial_kwh':float(battery),'backup_capacity_kwh':float(battery),
               'demand_kwh':0.,'consumed_kwh':0.,'grid_kwh':0.,'backup_kwh':0.,'charged_kwh':0.,'unserved_kwh':0.,
               'critical_service_fraction':1.,'transport_service_fraction':1.,'traction_demand_kw':0.,
               'traction_served_kw':0.,'traction_kwh':0.,'traction_demand_kwh':0.,'end_uses_kw':{},'status':'normal'},
      'elevators':{'assets':assets,'queue_people':0,'in_car_people':0,'requests_people':0,
                  'completed_passengers':0,'trips':0,'energy_kwh':0.,'availability_pct':100.,'status':'normal'},
      'internet':{'demand_mbps':0.,'served_mbps':0.,'capacity_mbps':float(mbps),'available_capacity_mbps':float(mbps),'latency_ms':None,
                  'transferred_gb':0.,'unserved_gb':0.,'demand_gb':0.,'status':'normal'},
      'water':{'demand_m3_h':0.,'supply_m3_h':0.,'consumed_m3_h':0.,'tank_m3':tank*.65,
               'initial_tank_m3':tank*.65,'supply_available_m3_h':float(inlet),'tank_capacity_m3':float(tank),'supply_capacity_m3_h':float(inlet),
               'supplied_m3':0.,'consumed_m3':0.,'demand_m3':0.,'unserved_m3':0.,'status':'normal'},
      'sewer':{'inflow_m3_h':0.,'outflow_m3_h':0.,'capacity_m3_h':float(sewer_capacity),'available_capacity_m3_h':float(sewer_capacity),
               'backlog_m3':0.,'storage_capacity_m3':float(sewer_storage),'inflow_m3':0.,
               'treated_m3':0.,'overflow_m3':0.,'status':'normal'},
      '_elevator_request_carry':0.}


def _person_sites(state):
    """Assign each nearby person to at most one facility; moving vehicle riders excluded."""
    places=[PLACE_INDEX[ident] for ident in FACILITY_IDS]
    vehicles={v['id']:v for v in state.get('vehicles',[])}
    result={}
    for person in state.get('people',[]):
        if person.get('status')=='onboard':
            vehicle=vehicles.get(person.get('vehicle_id'),{})
            if vehicle.get('status')!='boarding':continue
        x,y=person['x'],person['y']
        nearest=None;best=450**2
        for place in places:
            distance=(place['x']-x)**2+(place['y']-y)**2
            if distance<best:nearest=place['id'];best=distance
        if nearest:result[person['id']]=nearest
    return result


def _merge_defaults(target,defaults):
    for key,value in defaults.items():
        if key not in target:target[key]=deepcopy(value)
        elif isinstance(value,dict) and isinstance(target[key],dict):_merge_defaults(target[key],value)


def ensure_infrastructure(state):
    """Additive idempotent migration; preserve travelers, existing meters and clock."""
    when=float(state['t'])
    if 'infrastructure' not in state:
        state['infrastructure']={'model_version':VERSION,'installed_at':_iso(when),'observed_at':_iso(when),
          'scope':SCOPE,'facilities':{ident:_initial_facility(ident) for ident in FACILITY_IDS},
          'disruptions':[],'history':[],'next_disruption':0,'observed_t':when,'last_history_t':when,
          'unobserved_seconds':0.,'_schema_revision':2,'_person_sites':_person_sites(state),
          '_vehicle_progress':{v['id']:v.get('progress',0.) for v in state.get('vehicles',[])},
          'systems':{mode:{'id':mode,'name':mode.title()+' traction','demand_kw':0.,'served_kw':0.,'consumed_kwh':0.} for mode in ('subway','rail')}}
        return state['infrastructure']
    infra=state['infrastructure']
    if infra.get('_schema_revision')==2 and infra.get('model_version')==VERSION and all(ident in infra.get('facilities',{}) for ident in FACILITY_IDS):
        return infra
    defaults={'model_version':VERSION,'installed_at':_iso(when),'observed_at':_iso(when),'scope':SCOPE,
              'facilities':{},'disruptions':[],'history':[],'next_disruption':0,'observed_t':when,
              'last_history_t':when,'unobserved_seconds':0.,'_schema_revision':2,
              '_vehicle_progress':{v['id']:v.get('progress',0.) for v in state.get('vehicles',[])},
              'systems':{mode:{'id':mode,'name':mode.title()+' traction','demand_kw':0.,'served_kw':0.,'consumed_kwh':0.} for mode in ('subway','rail')},
              '_person_sites':infra.get('_person_sites',_person_sites(state))}
    _merge_defaults(infra,defaults)
    for ident in FACILITY_IDS:
        if ident not in infra['facilities']:infra['facilities'][ident]=_initial_facility(ident)
        else:_merge_defaults(infra['facilities'][ident],_initial_facility(ident))
    infra['_schema_revision']=2
    return infra


def _expire(infra,when):
    for disruption in infra['disruptions']:
        if disruption['status']=='active' and disruption['_ends_t']<=when:
            disruption.update(status='expired',restored_at=_iso(disruption['_ends_t']))


def _severity(infra,ident,utility,when):
    return max((d['severity'] for d in infra['disruptions'] if d['facility_id']==ident and d['utility']==utility
                and d['status']=='active' and d['_started_t']<=when<d['_ends_t']),default=0.)


def _classify(ratio):return 'critical' if ratio<.5 else ('warning' if ratio<.98 else 'normal')


def _activity(state,infra):
    current=_person_sites(state);previous=infra['_person_sites']
    counts=Counter(current.values());entries=Counter();exits=Counter();vehicles=Counter()
    for ident,site in current.items():
        if previous.get(ident)!=site:entries[site]+=1
    for ident,site in previous.items():
        if current.get(ident)!=site:exits[site]+=1
    for vehicle in state.get('vehicles',[]):
        nearest=min(FACILITY_IDS,key=lambda ident:(PLACE_INDEX[ident]['x']-vehicle['x'])**2+(PLACE_INDEX[ident]['y']-vehicle['y'])**2)
        place=PLACE_INDEX[nearest]
        if (place['x']-vehicle['x'])**2+(place['y']-vehicle['y'])**2<=350**2:vehicles[nearest]+=1
    infra['_person_sites']=current
    for ident,facility in infra['facilities'].items():
        activity=facility['activity']
        activity.update(present_people=counts[ident],entries_people=entries[ident],exits_people=exits[ident],vehicles_present=vehicles[ident])
        activity['entries_total_people']+=entries[ident];activity['exits_total_people']+=exits[ident]


def _traction_loads(state,infra,dt):
    """Authored kWh/km from observed route movement; equal station feeder allocation."""
    loads={ident:{'subway':0.,'rail':0.} for ident in FACILITY_IDS}
    previous=infra['_vehicle_progress'];current={}
    for vehicle in state.get('vehicles',[]):
        current[vehicle['id']]=vehicle.get('progress',0.)
        mode=vehicle.get('mode')
        if mode not in ('subway','rail') or vehicle['id'] not in previous:continue
        route=ROUTE_INDEX.get(vehicle.get('route_id'))
        if not route:continue
        distance=min(abs(current[vehicle['id']]-previous[vehicle['id']]),route['speed_mps']*dt)
        intensity=(2.4 if mode=='subway' else 4.2)*(.75+.25*vehicle.get('occupancy',0)/max(1,vehicle.get('capacity',1)))
        kw=distance/1000*intensity/(dt/3600)
        feeders=[ident for ident in route['stops'] if ident in loads]
        for ident in feeders:loads[ident][mode]+=kw/max(1,len(feeders))
    infra['_vehicle_progress']=current
    return loads


def _advance_facility(facility,infra,t,dt,traction):
    hours=dt/3600.;ident=facility['id'];activity=facility['activity'];profile=PROFILES[facility['kind']]
    base,_,_,_,_,_,_,_,water_base,_,_=profile
    failures={utility:_severity(infra,ident,utility,t) for utility in UTILITIES}
    lifts=facility['elevators'];assets=lifts['assets']
    # Vertical circulation requests are a deterministic subset of observed transitions.
    facility['_elevator_request_carry']+=(activity['entries_people']+activity['exits_people'])*.16
    requests=int(facility['_elevator_request_carry']);facility['_elevator_request_carry']-=requests
    available_count=max(0,len(assets)-math.ceil(failures['elevators']*len(assets)-1e-12))
    lifts['requests_people']+=requests
    # One facility waiting pool: reroute waiting passengers to available lifts.
    # People already in a disabled car remain there until that car recovers.
    waiting=requests+sum(asset['queue_people'] for asset in assets)
    for asset in assets:asset['queue_people']=0
    candidates=assets[:available_count] or assets
    for _ in range(waiting):
        asset=min(candidates,key=lambda a:(a['queue_people']+a['in_car_people'],a['id']))
        asset['queue_people']+=1
    for i,asset in enumerate(assets):
        if i>=available_count:
            asset['status']='out_of_service';continue
        if asset['remaining_seconds']<=1e-9 and asset['queue_people']:
            take=min(asset['capacity_people'],asset['queue_people'])
            asset['queue_people']-=take;asset['in_car_people']=take;asset['remaining_seconds']=asset['trip_seconds']
        asset['status']='moving' if asset['remaining_seconds']>0 else 'idle'
    requested_motor=sum(a['motor_kw'] for a in assets if a['status']=='moving')
    idle_kw=sum(a['idle_kw'] for a in assets)
    hour=datetime.fromtimestamp(t,TZ).hour
    operating=.68 if 1<=hour<5 else 1.
    # Baseline end uses plus activity; elevators are an explicitly metered subset.
    end_uses={'hvac':base*.36*operating+activity['present_people']*.04,
              'lighting':base*.24*operating,'equipment':base*.25+activity['vehicles_present']*3.,
              'hot_water':base*.15+activity['present_people']*.018,'elevators':requested_motor+idle_kw,
              'traction':sum(traction.values())}
    demand=sum(end_uses.values());power=facility['power']
    grid_available=power['grid_capacity_kw']*(1-failures['power'])
    grid_load=min(demand,grid_available)
    backup=min(max(0.,demand-grid_load),power['backup_capacity_kw'],power['backup_remaining_kwh']/hours)
    served=grid_load+backup;unserved=max(0.,demand-served)
    power['backup_remaining_kwh']-=backup*hours
    charging=min(max(0.,grid_available-grid_load),power['backup_capacity_kw']*.35,
                 max(0.,power['backup_capacity_kwh']-power['backup_remaining_kwh'])/hours)
    power['backup_remaining_kwh']+=charging*hours
    # Priority circuits preserve vertical circulation, controls, communications and pumps.
    critical_demand=base*.28+requested_motor+idle_kw
    critical_fraction=min(1.,served/max(.001,critical_demand))
    traction_served=min(end_uses['traction'],max(0.,served-critical_demand))
    traction_fraction=traction_served/end_uses['traction'] if end_uses['traction']>1e-8 else 1.
    power.update(available_grid_capacity_kw=grid_available,traction_demand_kw=end_uses['traction'],traction_served_kw=traction_served,
                 transport_service_fraction=min(critical_fraction,served/max(.001,demand)),demand_kw=demand,served_kw=served,grid_kw=grid_load+charging,backup_kw=backup,
                 charging_kw=charging,unserved_kw=unserved,critical_service_fraction=critical_fraction,
                 end_uses_kw=end_uses,status=_classify(served/max(.001,demand)))
    if failures['power'] and power['status']=='normal':power['status']='warning'
    for meter,value in [('demand_kwh',demand),('consumed_kwh',served),('grid_kwh',grid_load+charging),
                        ('backup_kwh',backup),('charged_kwh',charging),('unserved_kwh',unserved)]:
        power[meter]+=value*hours
    power['traction_kwh']+=traction_served*hours;power['traction_demand_kwh']+=end_uses['traction']*hours
    for mode,requested in traction.items():
        system=infra['systems'][mode]
        system['demand_kw']+=requested;system['served_kw']+=requested*traction_fraction
        system['consumed_kwh']+=requested*traction_fraction*hours
    for asset in assets:
        idle_energy=asset['idle_kw']*critical_fraction*hours
        motor_energy=0.
        if asset['status']=='moving':
            movement=min(asset['remaining_seconds'],dt*critical_fraction)
            asset['remaining_seconds']-=movement;motor_energy=asset['motor_kw']*movement/3600
            if critical_fraction<=1e-8:asset['status']='paused'
            if asset['remaining_seconds']<=1e-8:
                asset['remaining_seconds']=0.;asset['trips']+=1
                asset['completed_passengers']+=asset['in_car_people'];asset['in_car_people']=0
                asset['status']='idle'
        asset['energy_kwh']+=idle_energy+motor_energy
    lifts.update(queue_people=sum(a['queue_people'] for a in assets),in_car_people=sum(a['in_car_people'] for a in assets),
                 completed_passengers=sum(a['completed_passengers'] for a in assets),
                 trips=sum(a['trips'] for a in assets),energy_kwh=sum(a['energy_kwh'] for a in assets),
                 availability_pct=100*available_count/max(1,len(assets))*critical_fraction)
    lifts['status']=_classify(lifts['availability_pct']/100)
    if lifts['queue_people']>len(assets)*24 and lifts['status']=='normal':lifts['status']='warning'
    internet=facility['internet']
    internet_demand=8.+activity['present_people']*.65+activity['vehicles_present']*1.8+requests*.25
    effective_capacity=internet['capacity_mbps']*(1-failures['internet'])*critical_fraction
    served_mbps=min(internet_demand,effective_capacity)
    utilization=served_mbps/max(.001,effective_capacity)
    latency=None if served_mbps<=1e-8 else min(2500.,4.+8.*utilization/max(.005,1-utilization))
    internet.update(available_capacity_mbps=effective_capacity,demand_mbps=internet_demand,served_mbps=served_mbps,latency_ms=latency,
                    status=_classify(served_mbps/max(.001,internet_demand)))
    if failures['internet'] and internet['status']=='normal':internet['status']='warning'
    internet['demand_gb']+=internet_demand*dt/8000
    internet['transferred_gb']+=served_mbps*dt/8000
    internet['unserved_gb']+=max(0.,internet_demand-served_mbps)*dt/8000
    water=facility['water']
    water_demand=water_base+activity['present_people']*.0015+activity['entries_people']*.0018/hours
    wanted=water_demand*hours
    inflow_capacity=water['supply_capacity_m3_h']*(1-failures['water'])*critical_fraction
    supplied=min(inflow_capacity*hours,max(0.,water['tank_capacity_m3']-water['tank_m3'])+wanted)
    consumed=min(wanted,water['tank_m3']+supplied)
    water['tank_m3']=max(0.,min(water['tank_capacity_m3'],water['tank_m3']+supplied-consumed))
    water.update(supply_available_m3_h=inflow_capacity,demand_m3_h=water_demand,supply_m3_h=supplied/hours,consumed_m3_h=consumed/hours,
                 status=_classify(consumed/max(.000001,wanted)))
    if (failures['water'] or water['tank_m3']<water['tank_capacity_m3']*.15) and water['status']=='normal':water['status']='warning'
    water['supplied_m3']+=supplied;water['consumed_m3']+=consumed;water['demand_m3']+=wanted;water['unserved_m3']+=wanted-consumed
    sewer=facility['sewer']
    inflow=consumed*.92+.03*hours
    out_capacity=sewer['capacity_m3_h']*(1-failures['sewer'])*critical_fraction
    outflow=min(sewer['backlog_m3']+inflow,out_capacity*hours)
    unstored=max(0.,sewer['backlog_m3']+inflow-outflow)
    overflow=max(0.,unstored-sewer['storage_capacity_m3'])
    sewer['backlog_m3']=min(unstored,sewer['storage_capacity_m3'])
    sewer.update(available_capacity_m3_h=out_capacity,inflow_m3_h=inflow/hours,outflow_m3_h=outflow/hours,
                 status='critical' if overflow>0 else ('warning' if failures['sewer'] or sewer['backlog_m3']>sewer['storage_capacity_m3']*.25 else 'normal'))
    sewer['inflow_m3']+=inflow;sewer['treated_m3']+=outflow;sewer['overflow_m3']+=overflow
    statuses=[facility[utility]['status'] for utility in UTILITIES]
    facility['status']='critical' if 'critical' in statuses else ('warning' if 'warning' in statuses else 'normal')


def _summary(infra):
    facilities=list(infra['facilities'].values())
    sums={
      'power_demand_kw':('power','demand_kw'),'power_served_kw':('power','served_kw'),
      'power_unserved_kw':('power','unserved_kw'),'energy_kwh':('power','consumed_kwh'),
      'backup_kwh':('power','backup_kwh'),'traction_kw':('power','traction_served_kw'),
      'traction_kwh':('power','traction_kwh'),'elevator_trips':('elevators','trips'),
      'elevator_queue_people':('elevators','queue_people'),'internet_mbps':('internet','served_mbps'),
      'data_gb':('internet','transferred_gb'),'water_m3_h':('water','consumed_m3_h'),
      'water_consumed_m3':('water','consumed_m3'),'sewer_m3_h':('sewer','outflow_m3_h'),
      'sewer_backlog_m3':('sewer','backlog_m3'),'sewer_overflow_m3':('sewer','overflow_m3')}
    result={key:sum(f[utility][meter] for f in facilities) for key,(utility,meter) in sums.items()}
    result.update(active_disruptions=sum(d['status']=='active' for d in infra['disruptions']),
                  facilities_affected=sum(f['status']!='normal' for f in facilities),facilities=len(facilities))
    return result


def advance_infrastructure(state,dt):
    """Account once for [state.t, state.t+dt]; caller increments the main clock."""
    duration=_number(dt,'dt',0,86400)
    infra=ensure_infrastructure(state)
    if not duration:return infra
    start=float(state['t']);end=start+duration
    if infra['observed_t']>start+1e-6:raise ValueError('Infrastructure interval already accounted; advance the main clock before the next call')
    infra['unobserved_seconds']+=max(0.,start-infra['observed_t'])
    cursor=start
    traction=_traction_loads(state,infra,duration)
    while cursor<end-1e-8:
        _expire(infra,cursor)
        boundaries=[d['_ends_t'] for d in infra['disruptions'] if d['status']=='active' and cursor<d['_ends_t']<min(end,cursor+10)]
        finish=min([end,cursor+10]+boundaries)
        _activity(state,infra)
        for system in infra['systems'].values():system['demand_kw']=0.;system['served_kw']=0.
        for facility in infra['facilities'].values():_advance_facility(facility,infra,cursor,finish-cursor,traction[facility['id']])
        cursor=finish
        if cursor-infra['last_history_t']>=60-1e-8:
            values=_summary(infra)
            infra['history'].append({'time':_iso(cursor),**{key:values[key] for key in (
               'power_demand_kw','power_served_kw','energy_kwh','elevator_queue_people','internet_mbps','water_m3_h','sewer_backlog_m3')}})
            infra['history']=infra['history'][-360:];infra['last_history_t']=cursor
    _expire(infra,end)
    infra['observed_t']=end;infra['observed_at']=_iso(end)
    return infra


def create_disruption(state,facility_id,utility,duration_minutes,severity):
    if facility_id not in FACILITY_IDS:raise LookupError('Unknown infrastructure facility_id')
    if utility not in UTILITIES:raise ValueError('utility must be power, elevators, internet, water or sewer')
    minutes=_number(duration_minutes,'duration_minutes',1,1440)
    severity=_number(severity,'severity',.01,1)
    infra=ensure_infrastructure(state);now=float(state['t']);_expire(infra,now)
    if any(d['status']=='active' and d['facility_id']==facility_id and d['utility']==utility for d in infra['disruptions']):
        raise ValueError('An active disruption already exists for this facility and utility; restore it first')
    infra['next_disruption']+=1
    disruption={'id':f'util-{state.get("run_id","run")}-{infra["next_disruption"]:06d}',
        'facility_id':facility_id,'utility':utility,'severity':severity,'started_at':_iso(now),
        'ends_at':_iso(now+minutes*60),'status':'active','_started_t':now,'_ends_t':now+minutes*60,
        'synthetic':True}
    infra['disruptions'].append(disruption)
    return {key:value for key,value in disruption.items() if not key.startswith('_')}


def restore_disruption(state,ident):
    infra=state.get('infrastructure')
    disruption=next((d for d in infra['disruptions'] if d['id']==ident),None) if infra else None
    if disruption is None:raise LookupError('Unknown infrastructure disruption id')
    if disruption['status']=='active':disruption.update(status='restored',restored_at=_iso(state['t']))
    return {key:value for key,value in disruption.items() if not key.startswith('_')}


def transport_service_factor(state,route_id):
    """Read-only previous-step electric service factor for routes serving affected stations."""
    infra=state.get('infrastructure');route=ROUTE_INDEX.get(route_id)
    if not infra or not route:return 1.
    fractions=[infra['facilities'][ident]['power']['transport_service_fraction']
               for ident in route['stops'] if ident in infra['facilities']]
    # Subway/rail rely on station controls. Surface routes retain degraded operation.
    factor=min(fractions,default=1.)
    return factor if route['mode'] in ('subway','rail') else .35+.65*factor


def infrastructure_snapshot(state):
    """Pure projection: never install, advance, expire, or alter archived state."""
    infra=state.get('infrastructure')
    if not infra:return {'model_version':VERSION,'installed_at':None,'observed_at':None,'scope':SCOPE,
                          'summary':{},'facilities':[],'disruptions':[],'history':[]}
    def public(value):
        if isinstance(value,dict):return {key:public(item) for key,item in value.items() if not key.startswith('_')}
        if isinstance(value,list):return [public(item) for item in value]
        return value
    return {'model_version':infra['model_version'],'installed_at':infra['installed_at'],'observed_at':infra['observed_at'],
            'scope':infra['scope'],'summary':_summary(infra),'facilities':[public(f) for f in infra['facilities'].values()],
            'disruptions':public(infra['disruptions']),'history':deepcopy(infra['history']),
            'systems':public(list(infra.get('systems',{}).values())),
            'unobserved_seconds':infra.get('unobserved_seconds',0.),'synthetic':True}
