"""Conservation, units, truthful migration, physical outages, and restart tests."""
from copy import deepcopy
from datetime import datetime
import json
import math
import unittest
from unittest.mock import patch

from app.catalog import PLACE_INDEX
from app.infrastructure import (ensure_infrastructure,advance_infrastructure,infrastructure_snapshot,
                                create_disruption,restore_disruption,transport_service_factor,FACILITY_IDS)


def empty_state(start='2026-07-18T08:00:00-03:00'):
    return {'t':datetime.fromisoformat(start).timestamp(),'run_id':'test-run','people':[],'vehicles':[]}


def step(state,seconds):
    advance_infrastructure(state,seconds)
    state['t']+=seconds


def add_people(state,count,site='northstar_airport',start=0):
    place=PLACE_INDEX[site]
    state['people'] += [{'id':f'test:{i}','x':place['x'],'y':place['y'],'status':'queued'} for i in range(start,start+count)]


class InfrastructureTests(unittest.TestCase):
    def assert_ledgers(self,state):
        for facility in state['infrastructure']['facilities'].values():
            p=facility['power'];w=facility['water'];s=facility['sewer'];e=facility['elevators'];i=facility['internet']
            self.assertAlmostEqual(p['demand_kwh'],p['consumed_kwh']+p['unserved_kwh'],places=7)
            self.assertAlmostEqual(p['consumed_kwh'],p['grid_kwh']-p['charged_kwh']+p['backup_kwh'],places=7)
            self.assertAlmostEqual(p['backup_remaining_kwh'],p['backup_initial_kwh']+p['charged_kwh']-p['backup_kwh'],places=7)
            self.assertGreaterEqual(p['backup_remaining_kwh'],-1e-8)
            self.assertLessEqual(p['backup_remaining_kwh'],p['backup_capacity_kwh']+1e-8)
            self.assertAlmostEqual(w['initial_tank_m3']+w['supplied_m3']-w['consumed_m3'],w['tank_m3'],places=7)
            self.assertAlmostEqual(w['demand_m3'],w['consumed_m3']+w['unserved_m3'],places=7)
            self.assertAlmostEqual(s['inflow_m3'],s['treated_m3']+s['backlog_m3']+s['overflow_m3'],places=7)
            self.assertAlmostEqual(i['demand_gb'],i['transferred_gb']+i['unserved_gb'],places=7)
            self.assertEqual(e['requests_people'],e['completed_passengers']+e['queue_people']+e['in_car_people'])
            self.assertAlmostEqual(e['energy_kwh'],sum(asset['energy_kwh'] for asset in e['assets']))
            self.assertLessEqual(e['energy_kwh'],p['consumed_kwh']+1e-7)
            self.assertGreaterEqual(w['tank_m3'],0)
            self.assertLessEqual(w['tank_m3'],w['tank_capacity_m3'])
            self.assertLessEqual(s['backlog_m3'],s['storage_capacity_m3'])

    def test_migration_is_idempotent_and_does_not_fabricate_history(self):
        state=empty_state('2026-09-20T18:30:00-03:00');state['started_at']='2026-07-18T08:00:00-03:00'
        before=deepcopy(state);ensure_infrastructure(state)
        self.assertEqual({k:v for k,v in state.items() if k!='infrastructure'},before)
        snapshot=infrastructure_snapshot(state)
        self.assertEqual(snapshot['installed_at'],'2026-09-20T18:30:00-03:00')
        self.assertEqual(snapshot['history'],[])
        self.assertEqual(snapshot['summary']['energy_kwh'],0)
        self.assertEqual(snapshot['summary']['data_gb'],0)
        self.assertEqual(snapshot['summary']['water_consumed_m3'],0)
        exact=deepcopy(state)
        with patch('app.infrastructure._person_sites',side_effect=AssertionError('Fast ensure must not scan people')):
            ensure_infrastructure(state)
        self.assertEqual(state,exact)
        self.assertEqual(len(snapshot['facilities']),12)
        self.assertEqual(sum(len(f['elevators']['assets']) for f in snapshot['facilities']),34)

    def test_snapshot_is_read_only_even_without_migration_or_after_expiry_time(self):
        state=empty_state();before=deepcopy(state)
        self.assertEqual(infrastructure_snapshot(state)['facilities'],[])
        self.assertEqual(state,before)
        outage=create_disruption(state,'northstar_airport','internet',1,1)
        state['t']+=100;before=deepcopy(state)
        result=infrastructure_snapshot(state)
        result['facilities'][0]['power']['grid_kw']=-999
        self.assertEqual(state,before)
        self.assertEqual(state['infrastructure']['disruptions'][0]['status'],'active')

    def test_kw_seconds_to_kwh_and_mbps_seconds_to_decimal_gb(self):
        state=empty_state();ensure_infrastructure(state)
        step(state,10)
        for facility in state['infrastructure']['facilities'].values():
            p=facility['power'];i=facility['internet']
            self.assertAlmostEqual(p['consumed_kwh'],p['served_kw']*10/3600)
            self.assertAlmostEqual(i['transferred_gb'],i['served_mbps']*10/8000)
        self.assert_ledgers(state)
        step(state,3590)
        self.assertEqual(len(state['infrastructure']['history']),60)
        self.assert_ledgers(state)

    def test_activity_increases_electricity_network_water_and_explicit_elevator_trips(self):
        state=empty_state();ensure_infrastructure(state);step(state,10)
        baseline=deepcopy(state['infrastructure']['facilities']['northstar_airport'])
        add_people(state,160);step(state,10)
        airport=state['infrastructure']['facilities']['northstar_airport']
        self.assertEqual(airport['activity']['entries_people'],160)
        self.assertGreater(airport['power']['demand_kw'],baseline['power']['demand_kw'])
        self.assertGreater(airport['internet']['demand_mbps'],baseline['internet']['demand_mbps'])
        self.assertGreater(airport['water']['demand_m3_h'],baseline['water']['demand_m3_h'])
        self.assertEqual(airport['elevators']['requests_people'],25)
        step(state,20)
        self.assertEqual(airport['elevators']['completed_passengers'],25)
        self.assertEqual(airport['elevators']['trips'],8)
        self.assert_ledgers(state)

    def test_elevator_outage_reassigns_waiters_and_preserves_people_already_in_car(self):
        state=empty_state();ensure_infrastructure(state)
        create_disruption(state,'northstar_airport','elevators',60,.5)
        add_people(state,400);step(state,10)
        assets=state['infrastructure']['facilities']['northstar_airport']['elevators']['assets']
        self.assertTrue(all(a['queue_people']==0 and a['in_car_people']==0 for a in assets[4:]))
        self.assertEqual(sum(a['in_car_people'] for a in assets[:4]),48)
        self.assert_ledgers(state)
        other=empty_state();ensure_infrastructure(other);add_people(other,160);step(other,10)
        outage=create_disruption(other,'northstar_airport','elevators',60,1)
        before=[a['in_car_people'] for a in other['infrastructure']['facilities']['northstar_airport']['elevators']['assets']]
        step(other,60)
        lifts=other['infrastructure']['facilities']['northstar_airport']['elevators']
        self.assertEqual([a['in_car_people'] for a in lifts['assets']],before)
        self.assertEqual(lifts['completed_passengers'],0)
        restore_disruption(other,outage['id']);step(other,20)
        self.assertEqual(lifts['completed_passengers'],25)
        self.assert_ledgers(other)

    def test_power_backup_depletes_then_recovers_and_controls_transport_factor(self):
        state=empty_state();ensure_infrastructure(state)
        airport=state['infrastructure']['facilities']['northstar_airport']
        power=airport['power'];power['backup_initial_kwh']=power['backup_remaining_kwh']=.1
        outage=create_disruption(state,'northstar_airport','power',60,1)
        step(state,10)
        self.assertGreater(power['backup_kwh'],0)
        self.assertGreater(power['unserved_kwh'],0)
        step(state,10)
        self.assertEqual(power['backup_remaining_kwh'],0)
        self.assertEqual(transport_service_factor(state,'metro_1'),0)
        self.assertEqual(airport['internet']['latency_ms'],None)
        self.assert_ledgers(state)
        restore_disruption(state,outage['id']);step(state,10)
        self.assertEqual(transport_service_factor(state,'metro_1'),1)
        self.assertGreater(power['charging_kw'],0)
        self.assert_ledgers(state)

    def test_water_buffer_and_sewer_overflow_conserve_volume(self):
        state=empty_state();ensure_infrastructure(state)
        airport=state['infrastructure']['facilities']['northstar_airport']
        water=airport['water'];water['initial_tank_m3']=water['tank_m3']=.01
        outage=create_disruption(state,'northstar_airport','water',60,1)
        step(state,60)
        self.assertEqual(water['tank_m3'],0)
        self.assertGreater(water['unserved_m3'],0)
        self.assert_ledgers(state)
        restore_disruption(state,outage['id'])
        create_disruption(state,'northstar_airport','sewer',120,1)
        step(state,7200)
        self.assertGreater(airport['sewer']['overflow_m3'],0)
        self.assert_ledgers(state)
        step(state,600)
        self.assertLess(airport['sewer']['backlog_m3'],airport['sewer']['storage_capacity_m3'])
        self.assert_ledgers(state)

    def test_outage_expiry_boundaries_restore_idempotence_and_validation(self):
        state=empty_state()
        outage=create_disruption(state,'northstar_airport','internet',1,1)
        with self.assertRaises(ValueError):create_disruption(state,'northstar_airport','internet',2,.5)
        step(state,60)
        self.assertEqual(state['infrastructure']['disruptions'][0]['status'],'expired')
        self.assertEqual(state['infrastructure']['facilities']['northstar_airport']['internet']['transferred_gb'],0)
        step(state,10)
        self.assertGreater(state['infrastructure']['facilities']['northstar_airport']['internet']['transferred_gb'],0)
        self.assertEqual(restore_disruption(state,outage['id']),restore_disruption(state,outage['id']))
        with self.assertRaises(LookupError):restore_disruption(state,'missing')
        with self.assertRaises(LookupError):create_disruption(state,'missing','power',1,1)
        with self.assertRaises(ValueError):create_disruption(state,'northstar_airport','unknown',1,1)
        for severity in (0,-1,math.inf,math.nan,True,1.1):
            with self.assertRaises(ValueError):create_disruption(state,'northstar_airport','power',1,severity)
        for minutes in (0,-1,1441,math.inf,math.nan,True):
            with self.assertRaises(ValueError):create_disruption(state,'northstar_airport','power',minutes,1)

    def test_restart_determinism_and_duplicate_interval_guard(self):
        state=empty_state();ensure_infrastructure(state);add_people(state,177)
        create_disruption(state,'northstar_airport','power',13,.87);step(state,37)
        restored=json.loads(json.dumps(state));step(state,777);step(restored,777)
        self.assertEqual(state,restored)
        self.assertEqual(infrastructure_snapshot(state),infrastructure_snapshot(restored))
        self.assert_ledgers(state)
        advance_infrastructure(state,10)
        with self.assertRaises(ValueError):advance_infrastructure(state,10)

    def test_missed_time_is_reported_without_meter_backfill(self):
        state=empty_state();ensure_infrastructure(state);state['t']+=3600
        step(state,10)
        self.assertEqual(state['infrastructure']['unobserved_seconds'],3600)
        for facility in state['infrastructure']['facilities'].values():
            self.assertAlmostEqual(facility['power']['consumed_kwh'],facility['power']['served_kw']*10/3600)
        self.assertEqual(len(state['infrastructure']['history']),1)

class TractionTests(unittest.TestCase):
    def test_measured_vehicle_distance_becomes_authored_traction_energy_and_system_totals(self):
        state=empty_state()
        state['vehicles']=[{'id':'metro-test','mode':'subway','route_id':'metro_1','progress':0.,
                            'x':-11500.,'y':9300.,'occupancy':210,'capacity':420,'status':'moving'}]
        ensure_infrastructure(state)
        state['vehicles'][0]['progress']=160.
        step(state,10)
        # 160 m at authored 2.4 kWh/km and 87.5% occupancy factor.
        expected=.160*2.4*.875
        snapshot=infrastructure_snapshot(state)
        self.assertAlmostEqual(snapshot['summary']['traction_kwh'],expected)
        self.assertAlmostEqual(snapshot['summary']['traction_kw'],expected*360)
        subway=next(system for system in snapshot['systems'] if system['id']=='subway')
        self.assertAlmostEqual(subway['consumed_kwh'],expected)
        total=sum(f['power']['traction_kwh'] for f in snapshot['facilities'])
        self.assertAlmostEqual(total,expected)
        self.assertTrue(all(f['power']['unserved_kw']==0 for f in snapshot['facilities']))
        step(state,10)
        self.assertEqual(infrastructure_snapshot(state)['summary']['traction_kw'],0)
        self.assertAlmostEqual(infrastructure_snapshot(state)['summary']['traction_kwh'],expected)

    def test_split_outage_boundary_does_not_discard_measured_traction(self):
        state=empty_state()
        state['vehicles']=[{'id':'rail-test','mode':'rail','route_id':'rail_regional','progress':0.,
                            'x':-11500.,'y':9300.,'occupancy':280,'capacity':280,'status':'moving'}]
        ensure_infrastructure(state)
        create_disruption(state,'northstar_airport','internet',1.25,.5)
        step(state,70)
        state['vehicles'][0]['progress']=200.
        step(state,10)
        self.assertAlmostEqual(infrastructure_snapshot(state)['summary']['traction_kwh'],.2*4.2)
