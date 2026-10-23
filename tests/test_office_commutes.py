"""Individual journeys, cap deferral, clocks and exposure; no live database/network."""
import copy
from datetime import datetime
import heapq
import json
from unittest.mock import patch

import pytest

from app import commutes, engine
from app.catalog import NODES, ROUTE_INDEX


def workplace(**changes):
    return {'id': 'aurora', 'name': 'Aurora Technology Centre',
        'location': {'crs': 'northstar-local-m-v1', 'units': 'm', 'x': 2700, 'y': 9000, 'district_id': 'central'},
        'source_sim_time': '2026-10-01T09:00:00-03:00', **changes}


def worker(**changes):
    return {'worker_id': 'aurora-worker-1', 'workplace_id': 'aurora', 'home_place_id': 'cedar_terrace',
        'role_group': 'office_staff', 'work_mode': 'office', 'commute_mode': 'car',
        'shift_start': '08:00', 'shift_end': '08:05', 'shift_days': [0,1,2,3,4],
        'roster_rotation': 'weekdays', 'roster_offset': 0, 'hybrid_days': [0,1,2],
        'source_sim_time': '2026-10-01T09:00:00-03:00', **changes}


def registry(*workers, office=None):
    place = office or workplace()
    return {'revision': 1, 'workplaces': {place['id']: place}, 'workers': {w['worker_id']: w for w in workers}}


def state(start='2026-07-20T07:00:00-03:00'):
    result = engine.create_state(start=start)
    result['people'] = []; result['generated_people'] = 0; result['demand_requested'] = 0
    for vehicle in result['vehicles']:
        vehicle['occupancy'] = 0
    result['incident_threshold'] = 1000000
    return result


def step(result, seconds, definitions):
    with patch('app.engine._demand'), patch('app.engine._commute_demand',create=True), patch('app.engine._endogenous_incidents'):
        engine.advance(result, seconds, definitions)


def test_outbound_and_return_are_real_moving_agents_with_one_stable_identity():
    result = state(); definitions = registry(worker())
    commutes.prepare(result, definitions)
    departure = result['commute_events'][0][0]
    result['t'] = departure
    step(result, 10, definitions)
    assert len(result['people']) == 1
    person = result['people'][0]; ident = person['id']
    assert person['office_worker_id'] == worker()['worker_id']
    assert person['commute_direction'] == 'outbound'
    assert person['destination_id'] == 'office:aurora'
    assert person['source_sim_time'].startswith('2026-10-01')
    assert datetime.fromtimestamp(result['t'], commutes.TZ).month == 7
    first_position = (person['x'], person['y'])
    step(result, 60, definitions)
    assert (person['x'], person['y']) != first_position
    step(result, 3000, definitions)
    history = result['commute_history']
    assert [entry['direction'] for entry in history] == ['outbound', 'return']
    assert history[0]['person_id'] == history[1]['person_id'] == ident
    assert history[0]['trip_id'] != history[1]['trip_id']
    assert len(result['people']) == 0
    assert result['commute_at_work'] == {}
    assert result['generated_people'] == result['completed_trips'] == 2
    assert len(result['commute_leg_history']) == 2
    assert all(leg['worker_id'] == worker()['worker_id'] for leg in result['commute_leg_history'])


def test_registration_does_not_replay_past_outbound_or_invent_return():
    result = state('2026-07-20T09:00:00-03:00'); definitions = registry(worker(shift_end='17:00'))
    commutes.prepare(result, definitions)
    assert datetime.fromtimestamp(result['commute_events'][0][0], commutes.TZ).day == 21
    step(result, 3600, definitions)
    assert not result['people'] and not result['commute_at_work']
    assert result['commute_totals']['materialized'] == 0


def test_remote_and_hybrid_work_days_do_not_generate_daily_office_trips():
    result = state(); definitions = registry(worker(work_mode='remote'), worker(worker_id='hybrid',work_mode='hybrid',hybrid_days=[2]))
    commutes.prepare(result, definitions)
    assert len(result['commute_events']) == 1
    assert datetime.fromtimestamp(result['commute_events'][0][0], commutes.TZ).weekday() == 2


def test_overnight_rotation_spans_midnight_and_days_off_are_real():
    definition = worker(role_group='physical_security',shift_start='22:00',shift_end='06:00',shift_days=list(range(7)),roster_rotation='six_on_two_off',roster_offset=3)
    event = commutes.next_departure(definition,workplace(),datetime(2026,7,20,12,tzinfo=commutes.TZ).timestamp())
    assert event[3] - event[2] == 8*3600
    day = datetime.fromtimestamp(event[2],commutes.TZ).date()
    assert commutes.works_on(definition,day)
    from datetime import timedelta
    assert sum(commutes.works_on(definition,day+timedelta(days=i)) for i in range(8)) == 6


def test_cap_holds_real_deferred_requests_then_materializes_once():
    result = state(); definitions = registry(worker())
    commutes.prepare(result,definitions); result['t'] = result['commute_events'][0][0]
    result['max_people'] = 1
    result['people'] = [{'id':'existing'}]
    commutes.release(result,10,definitions)
    assert result['commute_totals']['requested'] == 1
    assert result['commute_totals']['materialized'] == 0
    assert len(result['commute_pending']) == 1
    assert result['people'] == [{'id':'existing'}]
    result['people'].clear();commutes.release(result,10,definitions)
    assert len(result['people']) == 1 and not result['commute_pending']
    commutes.release(result,10,definitions)
    assert len(result['people']) == 1


def test_expired_deferred_shift_counts_missed_and_no_fabricated_return():
    result = state(); definitions = registry(worker());commutes.prepare(result,definitions)
    result['t'] = result['commute_events'][0][3]+1;result['max_people'] = 0
    commutes.release(result,10,definitions)
    assert result['commute_totals']['missed'] == 1
    assert not result['commute_pending'] and not result['commute_at_work']
    assert result['commute_history'][0]['reason'] == 'shift_ended_before_capacity_available'
    assert not any(e[4]=='return' for e in result['commute_events'])


def test_commute_calendar_and_agents_survive_json_restart_without_duplicates():
    result = state(); definitions = registry(worker());commutes.prepare(result,definitions)
    result['t'] = result['commute_events'][0][0];step(result,70,definitions)
    restarted = json.loads(json.dumps(result))
    step(result,500,definitions);step(restarted,500,definitions)
    assert result == restarted


def test_ordinary_steps_never_rescan_future_roster():
    result = state(); definitions = registry(worker());commutes.prepare(result,definitions)
    with patch('app.commutes.next_departure',side_effect=AssertionError('roster rescanned')):
        step(result,120,definitions)


def test_rural_office_connector_preserves_graph_and_discloses_no_transit_coverage():
    before = copy.deepcopy(NODES)
    rural = workplace(id='rural',location={'x':-15000,'y':42000,'district_id':'rural_north'})
    definition = worker(workplace_id='rural',home_place_id='elm_crossing',commute_mode='transit')
    legs,duration,fallback = commutes.itinerary(definition,rural,'outbound')
    assert fallback == 'no_scheduled_public_transit_coverage'
    assert legs[0]['mode'] == 'car'
    assert legs[0]['destination_xy'] == [-15000.0,42000.0]
    points,measure = commutes.coordinate_path(tuple(legs[0]['origin_xy']),tuple(legs[0]['destination_xy']))
    assert points[-1] == [-15000.0,42000.0] and duration > 0 and measure[-1] > 0
    assert NODES == before


def test_transit_is_access_queue_real_vehicle_egress_same_person():
    # Find a real existing single-route itinerary using its existing stops.
    chosen = None
    for home in commutes.HOME_PLACES:
        for route in ROUTE_INDEX.values():
            for stop in route['stops']:
                target = engine.PLACE_INDEX[stop]
                office = workplace(location={'x':target['x']+20,'y':target['y']+20,'district_id':target['district_id']})
                w = worker(home_place_id=home,commute_mode='transit',shift_end='17:00')
                legs,_,fallback = commutes.itinerary(w,office,'outbound')
                if len(legs)==3:
                    chosen = (w,office,legs);break
            if chosen:break
        if chosen:break
    assert chosen
    w,office,legs = chosen;result=state();definitions=registry(w,office=office)
    commutes.prepare(result,definitions);result['t']=result['commute_events'][0][0]
    commutes.release(result,10,definitions);person=result['people'][0];ident=person['id']
    assert person['mode']=='walk'
    # Completing access does not complete the person, it creates the actual stop queue.
    engine._finish(result,person)
    assert person['id']==ident and person['status']=='queued' and result['completed_trips']==0
    middle=legs[1];route=ROUTE_INDEX[middle['route_id']]
    vehicle=next(v for v in result['vehicles'] if v['route_id']==route['id'])
    vehicle.update(next_stop=route['stops'].index(middle['origin_id']),direction=middle['direction'],occupancy=0)
    # At an interior stop, boarding follows this person's real direction.
    queues={(route['id'],middle['origin_id'],middle['direction']):[person]}
    engine._service_stop(result,vehicle,queues,{})
    assert person['status']=='onboard' and vehicle['occupancy']==1
    vehicle['occupancy']=3
    commutes.observe_contacts(result,30)
    assert person['shared_vehicle_seconds']==30 and person['co_passenger_seconds']==60
    assert commutes.summary(result)['contact_exposure'][0]['passenger_seconds']==30
    vehicle['occupancy']=1;vehicle['next_stop']=route['stops'].index(middle['destination_id'])
    engine._service_stop(result,vehicle,{}, {vehicle['id']:[person]})
    assert person['mode']=='walk' and person['status']=='moving' and vehicle['occupancy']==0
    engine._finish(result,person)
    assert person['status']=='completed' and result['completed_trips']==1
    assert len(result['commute_leg_history'])==3


def test_collision_hazard_uses_moving_vehicles_not_queued_people_and_is_bounded():
    result=state();result['vehicles']=[];result['people']=[{'status':'queued','mode':'bus'}]*5000
    engine._endogenous_incidents(result,10)
    assert result['traffic_exposure']['last_collision_hazard_increment']==0
    result['people']=[{'status':'moving','mode':'car','x':2700,'y':9000,'route_id':None,'office_worker_id':f'w{i}','workplace_id':'aurora'} for i in range(200)]
    engine._endogenous_incidents(result,10)
    low=result['traffic_exposure']['last_collision_hazard_increment']
    result['people']*=5;engine._endogenous_incidents(result,10)
    assert result['traffic_exposure']['last_collision_hazard_increment']>low
    for _ in range(5):
        result['traffic_exposure']['collision_threshold']=0.0000001
        engine._endogenous_incidents(result,10)
    collisions=[i for i in result['incidents'] if i['type']=='collision']
    assert len(collisions)==2
    assert all(0<=i['casualties']<=3 and i['requires_911'] for i in collisions)
    assert all(i['source']=='road_exposure_model' for i in collisions)
