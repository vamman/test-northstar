from copy import deepcopy
from app.weather_policy import neutral, DISTRICTS, VERSION
from app.environment_client import neutral as observation

def weather(peer, district=None, mild=False):
    p = neutral()
    p.update(pressure=.15 if mild else .95, level='advisory' if mild else 'closure',
        school_closed=not mild, remote_work_recommended=not mild,
        school_attendance_factor=.95 if mild else 0,
        work_attendance_factor=.97 if mild else .12,
        nonessential_travel_factor=.95 if mild else .10,
        essential_staff_factor=.99 if mild else .8,
        public_transit_service_factor=.95 if mild else .55)
    result=observation(peer,'connected')
    result['weather_response']={'schema_version':VERSION, 'regional':deepcopy(p),
        'districts':{d:deepcopy(p if district is None or d==district else neutral()) for d in DISTRICTS}}
    return result

from app import engine, commutes, weather_response

def test_severe_weather_suppresses_real_records_and_recovers():
    start=engine.create_state(start='2026-09-28T08:00:00-03:00')
    baseline, severe, mild=deepcopy(start),deepcopy(start),deepcopy(start)
    for state, w in [(baseline,None),(severe,weather('transit')),(mild,weather('transit',mild=True))]:
        engine.advance(state,600,environment=w)
    b,s,m=[x['weather_response_outcomes'] for x in (baseline,severe,mild)]
    assert s['generated_individual_trips'] < b['generated_individual_trips']*.45
    assert s['generated_road_vehicles'] < b['generated_road_vehicles']*.45
    assert m['generated_individual_trips'] > b['generated_individual_trips']*.7
    assert s['suppressed_road_vehicles']>100
    assert severe['road_traffic']['individual_school_trips']==start['road_traffic']['individual_school_trips']
    assert len(severe['people'])>0 # In-flight people remain, and may complete normally.
    engine.advance(severe,600,environment=observation('transit','unavailable'))
    assert severe['weather_response_outcomes']['suppressed_road_vehicles']==0
    assert severe['road_traffic']['individual_school_trips']>0


def test_origin_and_destination_districts_and_mild_weather():
    s=engine.create_state();s['environment']=weather('transit','north')
    assert weather_response.policy(s,'northstar_regional_hospital','central_station')['pressure']==0
    assert weather_response.policy(s,'university','central_station')['school_closed']
    assert weather_response.policy({'environment':weather('transit',mild=True)})['school_closed'] is False


def test_named_return_is_not_stranded_and_shared_worker_cohort():
    s=engine.create_state();s['environment']=weather('transit');weather_response.begin(s)
    home=next(iter(commutes.HOME_PLACES))
    workplace={'location':{'district_id':'central'}}
    worker={'worker_id':'worker-fixture','work_mode':'hybrid','role_group':'engineer','home_place_id':home}
    event=[s['t'],worker['worker_id'],s['t'],s['t']+28800,'outbound']
    first=weather_response.office_allowed(s,worker,workplace,event)
    assert first==weather_response.office_allowed(s,worker,workplace,event)
    event[-1]='return'
    assert weather_response.office_allowed(s,worker,workplace,event)
    assert s['weather_response_outcomes']['homebound_commutes_preserved']==1


def test_named_roster_actual_release_suppressed_and_return_completes():
    import heapq
    from test_office_commutes import registry, worker, state
    definitions=registry(*(worker(worker_id=f'office-test-{n}',shift_end='17:00') for n in range(100)))
    result=state('2026-09-28T07:00:00-03:00')
    commutes.prepare(result,definitions)
    result['t']=max(e[0] for e in result['commute_events'])
    base=deepcopy(result)
    result['environment']=weather('transit');weather_response.begin(result)
    weather_response.begin(base)
    commutes.release(base,10,definitions);commutes.release(result,10,definitions)
    assert len(result['people'])<len(base['people'])*.3
    assert result['commute_totals']['weather_suppressed']>70
    assert all(e['status']=='weather_suppressed' for e in result['commute_history'])
    assert not result['commute_at_work']
    # A known worker already at work must still get their real trip home.
    result['people']=[];result['commute_active_workers']={}
    event=base['people'][0]['commute_event'][:];event[-1]='return';event[0]=result['t']
    result['commute_at_work'][event[1]]={'arrived_at':'observed'}
    heapq.heappush(result['commute_events'],event)
    commutes.release(result,10,definitions)
    assert any(p['office_worker_id']==event[1] and p['commute_direction']=='return' for p in result['people'])
