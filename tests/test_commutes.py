"""Authored commute schedules, conservative road cohorts and physical motion."""
import copy
from datetime import datetime
import json
import math

from app.engine import (COMMUTE_PAIRS, HOME_PLACES, MAX_ROAD_COHORTS, ROAD_INDEX,
    ROAD_COHORT_WEIGHT, TZ, _advance_road_traffic, _commute_demand, _point, _traffic_path,
    _update_road_loads, advance, commute_profile, create_incident, create_state,
    ensure_road_traffic, road_traffic_snapshot, snapshot)


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=TZ).timestamp()


def test_weekday_commute_peaks_and_weekend_social_travel_are_distinct():
    morning=commute_profile(at('2026-09-28T08:00:00'))
    midday=commute_profile(at('2026-09-28T12:00:00'))
    evening=commute_profile(at('2026-09-28T17:00:00'))
    weekend=commute_profile(at('2026-10-03T08:00:00'))
    weekend_social=commute_profile(at('2026-10-03T14:00:00'))
    assert morning['rates']['work_out']>morning['rates']['work_home']*100
    assert evening['rates']['work_home']>evening['rates']['work_out']*100
    assert morning['vehicles_per_hour']>midday['vehicles_per_hour']
    assert evening['vehicles_per_hour']>midday['vehicles_per_hour']
    assert morning['vehicles_per_hour']>weekend['vehicles_per_hour']*2
    assert weekend_social['rates']['social']>weekend_social['rates']['work_home']*3
    assert weekend['weekday'] is False and weekend['school_in_session'] is False


def test_school_calendar_has_seasonal_and_weekend_closure_and_afternoon_egress():
    for date in ('2026-07-20','2026-08-24','2026-12-21','2027-01-04','2026-10-03'):
        profile=commute_profile(at(date+'T08:00:00'))
        assert profile['school_in_session'] is False
        assert profile['rates']['school_out']==profile['rates']['school_home']==0
    morning=commute_profile(at('2026-09-28T08:00:00'))
    afternoon=commute_profile(at('2026-09-28T15:00:00'))
    assert morning['school_in_session'] is True
    assert morning['rates']['school_out']>morning['rates']['school_home']*100
    assert afternoon['rates']['school_home']>afternoon['rates']['school_out']*100
    assert afternoon['commute_period']=='School dismissal'


def test_individual_journeys_have_real_home_activity_direction_and_purpose():
    morning=create_state(start='2026-09-28T08:00:00-03:00')
    _commute_demand(morning,300)
    people=[p for p in morning['people'] if p.get('trip_purpose')]
    assert any(p['trip_purpose']=='work' for p in people)
    assert any(p['trip_purpose']=='school' for p in people)
    assert all(p['home_id'] in HOME_PLACES for p in people)
    assert all((p['home_id'],p['activity_place_id']) in COMMUTE_PAIRS[p['trip_purpose']] for p in people)
    assert all(p['commute_direction']=='outbound' for p in people)
    assert any(p.get('pending_transit') or p['status']=='queued' for p in people)
    evening=create_state(start='2026-09-28T17:00:00-03:00')
    _commute_demand(evening,300)
    returning=[p for p in evening['people'] if p.get('trip_purpose')=='work']
    assert returning and all(p['commute_direction']=='homebound' for p in returning)
    assert all(p.get('final_destination_id',p.get('pending_transit',{}).get('destination_id',p['destination_id']))==p['home_id'] for p in returning)


def test_warm_road_cohorts_are_bounded_distinct_and_follow_actual_roads():
    state=create_state(start='2026-09-28T08:00:00-03:00')
    individual_ids={p['id'] for p in state['people']}
    initial={v['id']:(v['x'],v['y']) for v in state['road_traffic']['vehicles']}
    assert 500<=len(initial)<=MAX_ROAD_COHORTS
    assert individual_ids.isdisjoint(initial)
    _advance_road_traffic(state,30)
    moved=0
    for vehicle in state['road_traffic']['vehicles']:
        points,lengths,_roads=_traffic_path(vehicle['origin_id'],vehicle['destination_id'])
        expected=_point(points,lengths,vehicle['distance'])
        assert math.dist(expected,(vehicle['x'],vehicle['y']))<1e-6
        a,b=ROAD_INDEX[vehicle['road_id']]['geometry']['coordinates']
        distance=math.dist(a,b)
        assert abs(math.dist(a,(vehicle['x'],vehicle['y']))+math.dist((vehicle['x'],vehicle['y']),b)-distance)<1e-6
        assert vehicle['vehicle_weight']==ROAD_COHORT_WEIGHT
        assert math.isfinite(vehicle['heading'])
        if vehicle['id'] in initial and initial[vehicle['id']]!=(vehicle['x'],vehicle['y']):moved+=1
    assert moved>400
    assert {p['id'] for p in state['people']}==individual_ids


def test_road_cohort_conservation_and_capacity_suppression_are_explicit():
    state=create_state(start='2026-09-28T08:00:00-03:00')
    advance(state,600)
    traffic=state['road_traffic']
    active=sum(v['vehicle_weight'] for v in traffic['vehicles'])
    assert active+traffic['completed_vehicle_journeys']==traffic['generated_vehicle_journeys']
    assert traffic['generated_vehicle_journeys']+traffic['suppressed_vehicle_journeys']==traffic['requested_vehicle_journeys']
    assert len(traffic['vehicles'])<=MAX_ROAD_COHORTS
    # An intense configured queue is visibly suppressed, never hidden by weights.
    traffic['carry']=MAX_ROAD_COHORTS*2
    _advance_road_traffic(state,1)
    assert traffic['suppressed_vehicle_journeys']>0
    assert len(traffic['vehicles'])<=MAX_ROAD_COHORTS
    assert state['generated_people']==len(state['people'])+state['completed_trips']


def test_segment_congestion_changes_with_demand_capacity_and_local_collision():
    state=create_state(start='2026-09-28T08:00:00-03:00')
    morning=copy.deepcopy(state['road_traffic']['roads'])
    assert len({r['congestion_pct'] for r in morning.values()})>10
    assert all(r['flow_vehicles_hour']>0 and r['capacity_vehicles_hour']>0 for r in morning.values())
    state['t']=at('2026-09-28T02:00:00');_update_road_loads(state)
    quiet=state['road_traffic']['roads']
    assert max(r['congestion_pct'] for r in morning.values())>max(r['congestion_pct'] for r in quiet.values())
    state['t']=at('2026-09-28T08:00:00');_update_road_loads(state)
    create_incident(state,'collision','central_station',2)
    _update_road_loads(state)
    assert any(r['capacity_vehicles_hour']<morning[ident]['capacity_vehicles_hour']
               and r['congestion_pct']>=morning[ident]['congestion_pct'] for ident,r in state['road_traffic']['roads'].items())


def test_restart_reproduces_road_motion_schedules_and_infrastructure_counters():
    state=create_state(seed=87,start='2026-09-28T07:55:00-03:00')
    advance(state,95)
    restored=json.loads(json.dumps(state))
    advance(state,180);advance(restored,180)
    assert state==restored
    assert road_traffic_snapshot(state)==road_traffic_snapshot(restored)


def test_existing_state_migration_is_additive_and_getter_never_initializes():
    state=create_state(start='2026-07-20T14:00:00-03:00')
    state.pop('road_traffic')
    original=copy.deepcopy(state)
    view=road_traffic_snapshot(state)
    assert view['initialized'] is False and view['vehicles']==[]
    assert state==original
    ensure_road_traffic(state)
    assert {k:v for k,v in state.items() if k!='road_traffic'}==original
    retained=copy.deepcopy(state['road_traffic'])
    ensure_road_traffic(state)
    assert state['road_traffic']==retained
    assert 300<len(retained['vehicles'])<=MAX_ROAD_COHORTS
    assert not any(v['purpose']=='school' for v in retained['vehicles'])
    assert any(v['mode']=='taxi' for v in retained['vehicles'])


def test_snapshot_counts_keep_dot_sample_and_aggregate_occupancy_separate():
    state=create_state(start='2026-09-28T08:00:00-03:00')
    original=copy.deepcopy(state)
    view=snapshot(state)
    assert state==original
    road=view['road_traffic'];summary=road['summary']
    assert summary['active_individual_travelers']==view['stats']['active_people']==len(view['people'])
    assert summary['represented_vehicles']==len(road['vehicles'])*ROAD_COHORT_WEIGHT
    assert summary['represented_people']>summary['represented_vehicles']
    assert len(road['profile'])==24
    assert len(road['history'])<=180
    assert 'do not add' in ' '.join(road['assumptions'])
    assert 'distance' not in road['vehicles'][0]


def test_road_sensor_proxies_respond_without_inflating_individual_footfall():
    state=create_state(start='2026-09-28T08:00:00-03:00')
    before={s['id']:s for s in snapshot(state)['sensors']}
    state['road_traffic']['vehicles']=[]
    after={s['id']:s for s in snapshot(state)['sensors']}
    assert all(s['value']==after[ident]['value'] for ident,s in before.items() if s['kind']=='footfall')
    assert any(s['value']>after[ident]['value'] for ident,s in before.items() if s['kind']=='air_quality')
    assert all('weighted road' in s['derived_from'] for s in before.values())
