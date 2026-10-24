"""Cruise terminal layer checks: lifecycle, conservation, determinism, scenarios.

Pure engine-level tests; no database. The API surface is covered in test_api.py.
"""
import json
import math
import unittest

from app import cruise_terminal
from app.engine import advance, create_state, snapshot

# 18 July 2026 is a peak-season Saturday: MV Aurora Atlantic 08:02 plus MV Northern Star 10:30.
START = '2026-07-18T08:00:00-03:00'


class CruiseTerminalTests(unittest.TestCase):
    def test_terminal_initializes_additively_and_lists_scheduled_calls(self):
        state = create_state(start=START)
        self.assertIn('cruise_terminal', state)
        self.assertTrue(state['cruise_terminal']['rng'])
        summary = snapshot(state)['cruise_terminal']
        self.assertTrue(summary['enabled'])
        self.assertEqual(summary['port'], 'northstar_harbour')
        self.assertEqual([ship['status'] for ship in summary['ships']], ['scheduled', 'scheduled'])
        self.assertEqual(summary['ships'][0]['ship'], 'MV Aurora Atlantic')
        self.assertGreater(summary['ships'][0]['modeled'], 0)

    def test_call_lifecycle_releases_groups_boards_and_conserves(self):
        state = create_state(start=START)
        cruise_terminal.apply_scenario(state, shore_minutes=60, boarding_minutes=20)
        advance(state, 600)  # 08:10 — first call berthed and disembarking.
        summary = snapshot(state)['cruise_terminal']
        first = summary['ships'][0]
        self.assertEqual(first['status'], 'disembarking')
        self.assertEqual(first['berth'], 1)
        self.assertGreater(first['disembarked_people'], 0)
        self.assertLessEqual(first['disembarked_people'], first['modeled'])
        self.assertGreater(summary['ashore_people'], 0)
        advance(state, 3 * 3600)  # Past the compressed departure near 10:02.
        summary = snapshot(state)['cruise_terminal']
        first = summary['ships'][0]
        self.assertEqual(first['status'], 'departed')
        self.assertEqual(first['boarded_people'] + first['missed_people'], first['disembarked_people'])
        self.assertGreater(first['boarded_people'], first['missed_people'])
        self.assertGreater(first['return_rate_pct'], 90)
        self.assertGreater(summary['totals']['spend'], 0)
        self.assertTrue(any(destination['visitors'] > 0 for destination in summary['destinations']))
        for destination in summary['destinations']:
            self.assertLessEqual(destination['occupancy_people'], destination['capacity'])

    def test_group_positions_stay_near_the_harbour_corridor(self):
        state = create_state(start=START)
        advance(state, 1200)
        port_x, port_y = cruise_terminal.PORT_XY
        for group in state['cruise_terminal']['groups']:
            self.assertTrue(math.isfinite(group['x']) and math.isfinite(group['y']))
            self.assertLessEqual(math.hypot(group['x'] - port_x, group['y'] - port_y), 5000)
        states = {group['state'] for group in state['cruise_terminal']['groups']}
        self.assertTrue(states <= {'customs_queue', 'taxi_queue', 'shuttle_queue', 'transit',
                                   'at_destination', 'overflow_wait', 'security_queue'})

    def test_single_berth_scenario_holds_the_second_ship(self):
        state = create_state(start=START)
        cruise_terminal.apply_scenario(state, berths=1)
        advance(state, 9000)  # 10:30 — second call arrives, first still ashore.
        statuses = {ship['ship']: ship['status'] for ship in snapshot(state)['cruise_terminal']['ships']}
        self.assertEqual(statuses['MV Aurora Atlantic'], 'ashore')
        self.assertEqual(statuses['MV Northern Star'], 'holding')

    def test_reduced_customs_capacity_backs_up_the_queue(self):
        fast = create_state(start=START)
        cruise_terminal.apply_scenario(fast, customs_lanes=12)
        slow = create_state(start=START)
        cruise_terminal.apply_scenario(slow, customs_lanes=1)
        advance(fast, 900)
        advance(slow, 900)
        fast_queue = snapshot(fast)['cruise_terminal']['queues']['customs_people']
        slow_queue = snapshot(slow)['cruise_terminal']['queues']['customs_people']
        self.assertGreater(slow_queue, fast_queue)

    def test_scenario_validation_rejects_unknown_or_out_of_range_values(self):
        state = create_state(start=START)
        with self.assertRaises(ValueError):
            cruise_terminal.apply_scenario(state, berths=99)
        with self.assertRaises(ValueError):
            cruise_terminal.apply_scenario(state, nonsense=1)
        with self.assertRaises(ValueError):
            cruise_terminal.apply_scenario(state, shore_minutes=True)

    def test_json_restart_preserves_terminal_state_exactly(self):
        state = create_state(seed=7, start=START)
        advance(state, 900)
        restored = json.loads(json.dumps(state))
        advance(state, 1800)
        advance(restored, 1800)
        self.assertEqual(state['cruise_terminal'], restored['cruise_terminal'])
        self.assertEqual(snapshot(state)['cruise_terminal'], snapshot(restored)['cruise_terminal'])

    def test_summary_reports_throughput_all_aboard_and_insights(self):
        state = create_state(start=START)
        cruise_terminal.apply_scenario(state, shore_minutes=60, boarding_minutes=20)
        advance(state, 600)
        summary = snapshot(state)['cruise_terminal']
        throughput = summary['throughput']
        self.assertTrue(set(throughput) == {'disembarked_per_hour', 'boarded_per_hour', 'window_minutes'})
        self.assertTrue(all(value is None or value >= 0 for value in throughput.values()))
        self.assertLessEqual(len(summary['insights']), 8)
        advance(state, 3000)  # 09:00 — first call ashore, boarding not yet open.
        summary = snapshot(state)['cruise_terminal']
        self.assertIsNotNone(summary['all_aboard'])
        self.assertEqual(summary['all_aboard']['ship'], 'MV Aurora Atlantic')
        self.assertGreaterEqual(summary['all_aboard']['minutes_to_departure'], 0)
        self.assertGreaterEqual(summary['all_aboard']['minutes_to_boarding_start'], 0)
        for insight in summary['insights']:
            self.assertIn(insight['severity'], ('info', 'watch', 'alert'))
            self.assertTrue(insight['title'] and insight['detail'])

    def test_all_aboard_alert_fires_while_boarding_with_ashore_passengers(self):
        state = create_state(start=START)
        cruise_terminal.apply_scenario(state, shore_minutes=60, boarding_minutes=20)
        advance(state, 600)
        # Force the boarding window open early to exercise the advisory path.
        state['cruise_terminal']['visits'][0]['boarding_start_ts'] = state['t'] - 60
        state['cruise_terminal']['visits'][0]['status'] = 'boarding'
        summary = snapshot(state)['cruise_terminal']
        titles = ' '.join(insight['title'] for insight in summary['insights'])
        self.assertIn('All aboard', titles)

    def test_terminal_layer_never_consumes_the_main_rng_stream(self):
        with_terminal = create_state(seed=11, start=START)
        advance(with_terminal, 300)
        # The engine people sample must be unaffected by terminal group activity:
        # same seed and start reproduce identical engine state in a run where the
        # terminal is disabled by pruning its sub-state before advancing.
        reference = create_state(seed=11, start=START)
        del reference['cruise_terminal']
        advance(reference, 300)
        self.assertEqual(with_terminal['people'], reference['people'])
        self.assertEqual(with_terminal['vehicles'], reference['vehicles'])
        self.assertEqual(with_terminal['rng_state'], reference['rng_state'])


if __name__ == '__main__':
    unittest.main()
