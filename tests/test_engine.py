"""Behavioral checks for motion, conservation, persistence and integration semantics."""
import copy
import json
import math
import unittest

from app.catalog import CITY, EVENTS, PLACE_INDEX, ROUTE_INDEX, WORLD, district_at, road_path
from app.engine import (create_state, advance, snapshot, create_incident, apply_feedback,
                        _path, _point)


class EngineTests(unittest.TestCase):
    def test_shared_anchors_dense_map_and_annual_calendar(self):
        for name,x,y in [('northstar_airport',15500,11500),('northstar_rail',-11500,9300),('northstar_harbour',26500,22500)]:
            self.assertEqual((PLACE_INDEX[name]['x'],PLACE_INDEX[name]['y']),(x,y))
        self.assertGreater(len(CITY['buildings']),1500)
        self.assertGreater(len(CITY['roads']),1000)
        self.assertEqual(sum(r['mode']=='subway' for r in CITY['routes']),1)
        self.assertEqual({event['month'] for event in EVENTS},set(range(1,13)))
        self.assertGreaterEqual(len(EVENTS),24)
        for place in CITY['places']:
            self.assertEqual(place['district_id'],district_at(place['x'],place['y']))

    def test_seed_is_a_real_bounded_population_and_positions_follow_routes(self):
        state=create_state()
        self.assertTrue(1500<=len(state['people'])<=4000)
        advance(state,120)
        vehicles={v['id']:v for v in state['vehicles']}
        for p in state['people']:
            if p['status']=='moving':
                points,measure=_path(p['origin_id'],p['destination_id'])
                x,y=_point(points,measure,p['distance'])
            elif p['status']=='queued':
                origin=PLACE_INDEX[p['origin_id']];x,y=origin['x'],origin['y']
            else:x,y=vehicles[p['vehicle_id']]['x'],vehicles[p['vehicle_id']]['y']
            self.assertAlmostEqual(p['x'],x,places=7)
            self.assertAlmostEqual(p['y'],y,places=7)
        for vehicle in state['vehicles']:
            occupants=sum(p['vehicle_id']==vehicle['id'] and p['status']=='onboard' for p in state['people'])
            self.assertEqual(occupants,vehicle['occupancy'])
            self.assertLessEqual(occupants,vehicle['capacity'])
        self.assertEqual(state['generated_people'],len(state['people'])+state['completed_trips'])

    def test_json_restart_preserves_future_motion_and_randomness(self):
        state=create_state(seed=99)
        advance(state,173)
        restored=json.loads(json.dumps(state))
        advance(state,600);advance(restored,600)
        self.assertEqual(state,restored)
        self.assertEqual(snapshot(state),snapshot(restored))

    def test_arrivals_release_people_and_cruise_season_is_june_to_september(self):
        summer=create_state(start='2026-07-18T08:00:00-03:00')
        advance(summer,600)
        cruises=[a for a in summer['arrivals'] if a['mode']=='cruise' and a['status']=='disembarking']
        self.assertEqual(len(cruises),1)
        self.assertGreater(cruises[0]['generated_people'],0)
        winter=create_state(start='2026-01-18T08:00:00-04:00')
        self.assertFalse(any(a['mode']=='cruise' for a in winter['arrivals']))
        self.assertFalse(snapshot(winter)['stats']['cruise_season'])
        self.assertTrue(snapshot(winter)['clock']['iso'].endswith('-04:00'))
        self.assertTrue(snapshot(summer)['clock']['iso'].endswith('-03:00'))
        with self.assertRaises(ValueError):create_state(start='2026-07-18T08:00:00')

    def test_calendar_changes_destination_demand(self):
        state=create_state(start='2026-07-18T09:00:00-03:00')
        advance(state,1200)
        events=snapshot(state)['active_events']
        self.assertIn('jazz_weekend',[event['id'] for event in events])
        self.assertGreater(state['event_generated'].get('2026:jazz_weekend',0),0)
        self.assertTrue(any(p['event_id']=='2026:jazz_weekend' for p in state['people']))

    def test_capacity_suppression_is_explicit_and_conserves_generated_people(self):
        state=create_state()
        state['people']=[];state['generated_people']=0;state['demand_requested']=0
        state['max_people']=5
        for vehicle in state['vehicles']:vehicle['occupancy']=0
        advance(state,600)
        self.assertLessEqual(len(state['people']),5)
        self.assertGreater(state['suppressed_demand'],0)
        self.assertEqual(state['generated_people']+state['suppressed_demand'],state['demand_requested'])
        self.assertEqual(state['generated_people'],state['completed_trips']+len(state['people']))
        for arrival in state['arrivals']:
            self.assertEqual(arrival['processed_people'],arrival['generated_people']+arrival['suppressed_people'])

    def test_signal_stops_vehicle_and_feedback_recovers_without_regression(self):
        state=create_state()
        incident=create_incident(state,'signal_failure','central_station',route_id='metro_1')
        before={v['id']:v['progress'] for v in state['vehicles'] if v['route_id']=='metro_1'}
        advance(state,60)
        self.assertEqual(before,{v['id']:v['progress'] for v in state['vehicles'] if v['route_id']=='metro_1'})
        self.assertTrue(any(s['status']=='critical' and s['place_id']=='central_station' for s in snapshot(state)['sensors']))
        apply_feedback(state,{'incident_id':incident['id'],'source':'911','status':'on_scene'})
        apply_feedback(state,{'incident_id':incident['id'],'source':'911','status':'dispatching'})
        self.assertEqual(incident['status'],'on_scene')
        apply_feedback(state,{'incident_id':incident['id'],'source':'hospital','status':'resolved'})
        self.assertEqual(incident['status'],'on_scene')
        apply_feedback(state,{'incident_id':incident['id'],'source':'911','status':'resolved'})
        advance(state,60)
        self.assertNotEqual(before,{v['id']:v['progress'] for v in state['vehicles'] if v['route_id']=='metro_1'})
        apply_feedback(state,{'incident_id':incident['id'],'source':'911','status':'transporting'})
        self.assertEqual(incident['status'],'resolved')

    def test_incident_locations_identifiers_and_validation(self):
        state=create_state()
        incident=create_incident(state,'collision','northstar_airport',2)
        self.assertEqual(incident['location']['precision'],'facility')
        self.assertEqual(incident['location']['facility_id'],'northstar_airport')
        self.assertTrue(incident['requires_911'])
        other=create_state()
        self.assertNotEqual(incident['id'],create_incident(other,'collision','northstar_airport',2)['id'])
        for bad in [-1,31,True,1.5]:
            with self.assertRaises(ValueError):create_incident(state,'collision','northstar_airport',bad)
        with self.assertRaises(ValueError):create_incident(state,'unknown','northstar_airport')
        with self.assertRaises(ValueError):create_incident(state,'medical','unknown')
        with self.assertRaises(ValueError):apply_feedback(state,{'incident_id':'unknown','status':'resolved'})
        for seconds in [-1,math.nan,math.inf,True,86401]:
            with self.assertRaises(ValueError):advance(state,seconds)

    def test_exposure_events_and_durable_dispatch_never_self_resolve(self):
        state=create_state()
        state['incident_threshold']=.001
        advance(state,60)
        self.assertGreaterEqual(len(state['incidents']),1)
        incident=state['incidents'][0]
        self.assertEqual(incident['source'],'exposure_model')
        incident['dispatch_status']='sent'
        advance(state,600)
        self.assertNotIn(incident['status'],('resolved','closed'))


if __name__=='__main__':unittest.main()

class EventAndMaintenanceTests(unittest.TestCase):
    def test_event_egress_originates_at_venue(self):
        state=create_state(start='2026-07-18T18:00:00-03:00')
        advance(state,180)
        departing=[p for p in state['people'] if p.get('event_phase')=='departing']
        self.assertGreater(len(departing),0)
        self.assertTrue(all(p['origin_id']=='harbour_market' for p in departing))
        self.assertGreater(state['event_departures'].get('2026:harbour_market_day',0),0)

    def test_only_911_can_resolve_an_emergency_and_only_local_signal_repairs_automatically(self):
        state=create_state()
        emergency=create_incident(state,'medical','central_station',1)
        apply_feedback(state,{'incident_id':emergency['id'],'source':'traffic','status':'resolved'})
        self.assertEqual(emergency['status'],'reported')
        apply_feedback(state,{'incident_id':emergency['id'],'source':'hospital','status':'resolved'})
        self.assertEqual(emergency['clinical_status'],'resolved')
        self.assertEqual(emergency['status'],'reported')
        local=create_incident(state,'signal_failure','northstar_airport')
        dispatched=create_incident(state,'signal_failure','northstar_rail')
        dispatched['dispatch_status']='sent'
        advance(state,1220)
        self.assertEqual(local['status'],'resolved')
        self.assertEqual(local['resolution_source'],'transit_maintenance')
        self.assertEqual(dispatched['status'],'reported')
        self.assertEqual(emergency['status'],'reported')

class DateBoundaryRegressionTests(unittest.TestCase):
    def test_date_jump_does_not_replay_finished_or_elapsed_arrival_releases(self):
        finished=create_state(start='2026-07-18T08:55:00-03:00')
        flight=next(a for a in finished['arrivals'] if a['id']=='2026-07-18:air0')
        self.assertEqual(flight['status'],'completed')
        self.assertEqual(flight['before_start_people'],125)
        advance(finished,60)
        self.assertEqual(flight['generated_people'],0)
        midway=create_state(start='2026-07-18T08:10:00-03:00')
        flight=next(a for a in midway['arrivals'] if a['id']=='2026-07-18:air0')
        self.assertEqual(flight['before_start_people'],83)
        advance(midway,300)
        self.assertEqual(flight['generated_people'],42)
        self.assertEqual(flight['processed_people'],flight['generated_people']+flight['suppressed_people']+flight['before_start_people'])

    def test_arriving_visitors_do_not_target_a_departing_event(self):
        from unittest.mock import patch
        from app.engine import _arrival_trip
        state=create_state(start='2026-07-18T18:20:00-03:00')
        departing={'phase':'departing','venue_id':'harbour_market'}
        arrival={'place_id':'northstar_airport'}
        with patch('app.engine._event_instances',return_value=[departing]),patch('app.engine._transit_trip') as transit,patch('app.engine._direct_trip') as direct:
            for _ in range(50):_arrival_trip(state,arrival)
        destinations=[call.args[2] for call in list(transit.call_args_list)+list(direct.call_args_list)]
        self.assertNotIn('harbour_market',destinations)

    def test_service_pause_and_resume_and_no_new_queue_at_night(self):
        state=create_state(start='2026-07-18T01:20:00-03:00')
        before={v['id']:v['progress'] for v in state['vehicles']}
        created=state['next_person']
        advance(state,600)
        self.assertEqual(before,{v['id']:v['progress'] for v in state['vehicles']})
        self.assertTrue(all(v['status']=='out_of_service' for v in state['vehicles']))
        self.assertTrue(all(p['status']!='queued' for p in state['people'] if int(p['id'][1:])>created))
        morning=create_state(start='2026-07-18T04:59:00-03:00')
        before={v['id']:v['progress'] for v in morning['vehicles']}
        advance(morning,60)
        self.assertEqual(before,{v['id']:v['progress'] for v in morning['vehicles']})
        advance(morning,60)
        self.assertNotEqual(before,{v['id']:v['progress'] for v in morning['vehicles']})

    def test_all_event_instances_recur_across_years_including_midnight_egress(self):
        from datetime import datetime,timedelta
        from app.engine import _event_instances,TZ
        for event in EVENTS:
            for year in (2026,2027):
                start=datetime(year,event['month'],event['day'],event['start_hour'],tzinfo=TZ)
                end=start+timedelta(hours=event['duration_hours'])
                for phase,when in [('approaching',start-timedelta(minutes=30)),('active',start+timedelta(minutes=30)),('departing',end+timedelta(minutes=30))]:
                    matches=[e for e in _event_instances(when.timestamp()) if e['instance_id']==f'{year}:{event["id"]}']
                    self.assertEqual(len(matches),1)
                    self.assertEqual(matches[0]['phase'],phase)
                self.assertFalse(any(e['instance_id']==f'{year}:{event["id"]}' for e in _event_instances((end+timedelta(hours=1)).timestamp())))

class GatewayDisplayTests(unittest.TestCase):
    def test_gateway_positions_are_deterministic_timetable_paths_and_not_passenger_vehicles(self):
        from app.engine import _gateway_vehicles
        from app.catalog import contains
        state=create_state(start='2026-07-18T07:55:00-03:00')
        before=(len(state['people']),len(state['vehicles']),state['generated_people'])
        display=_gateway_vehicles(state)
        self.assertEqual({v['mode'] for v in display},{'aircraft','cruise'})
        self.assertTrue(all(v['status']=='approaching' for v in display))
        self.assertEqual(display,_gateway_vehicles(json.loads(json.dumps(state))))
        ring=WORLD['spatial_layers']['region']['geometry']['coordinates'][0]
        for v in display:
            self.assertTrue(contains(ring,v['x'],v['y']))
            self.assertTrue(math.isfinite(v['heading']))
        self.assertEqual(before,(len(state['people']),len(state['vehicles']),state['generated_people']))
        state['t']+=600
        at_gateway=_gateway_vehicles(state)
        self.assertTrue(all(v['status']=='disembarking' for v in at_gateway))
        for v in at_gateway:
            place=PLACE_INDEX[v['place_id']]
            self.assertEqual((v['x'],v['y']),(place['x'],place['y']))
        state['t']+=720
        departed=[v for v in _gateway_vehicles(state) if v['mode']=='aircraft']
        self.assertEqual(departed[0]['status'],'departing')
        self.assertNotEqual((departed[0]['x'],departed[0]['y']),(15500,11500))
