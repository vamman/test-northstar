"""Cruise terminal layer checks: lifecycle, conservation, determinism, scenarios.

Pure engine-level tests; no database. The API surface is covered in test_api.py.
Tests keep runtime low: short real advances for release/queue behavior, then
white-box schedule compression (overriding visit timestamps) for departure
paths instead of simulating whole days tick by tick.
"""
import json
import math
import unittest

from app import cruise_terminal
from app.engine import advance, create_state, snapshot

# 18 July 2026 is a peak-season Saturday: MV Aurora Atlantic 08:02 plus MV Northern Star 10:30.
START = '2026-07-18T08:00:00-03:00'
FAST_CLEAR = {'customs_seconds_per_group': 10, 'customs_lanes': 12,
              'security_seconds_per_group': 10, 'security_lanes': 12,
              'taxi_interval_seconds': 10}


class CruiseTerminalTests(unittest.TestCase):
    def test_destination_constants_are_bounded(self):
        for identifier, destination in cruise_terminal.DESTINATIONS.items():
            self.assertGreater(destination['capacity'], 0)
            low, high = destination['dwell_minutes']
            self.assertLessEqual(low, high)

    def test_config_limits_bracket_the_defaults(self):
        for key, value in cruise_terminal.DEFAULT_CONFIG.items():
            low, high = cruise_terminal.CONFIG_LIMITS[key]
            self.assertLessEqual(low, value <= high and value, high)

    def test_group_size_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(cruise_terminal.GROUP_SIZE_WEIGHTS), 1.0, places=6)

    def test_terminal_initializes_additively_and_lists_scheduled_calls(self):
        state = create_state(start=START)
        self.assertIn('cruise_terminal', state)
        self.assertTrue(state['cruise_terminal']['rng'])
        summary = snapshot(state)['cruise_terminal']
        self.assertTrue(summary['enabled'])
        self.assertEqual(summary['port'], 'northstar_harbour')
        # The engine timetable spans yesterday, today and tomorrow; check today's calls.
        today = [ship for ship in summary['ships'] if ship['arrival_iso'].startswith('2026-07-18')]
        self.assertEqual([ship['status'] for ship in today], ['scheduled', 'scheduled'])
        self.assertEqual(today[0]['ship'], 'MV Aurora Atlantic')
        self.assertTrue(all(ship['status'] == 'scheduled' for ship in summary['ships']))
        self.assertGreater(summary['ships'][0]['modeled'], 0)

    def test_disembarkation_releases_groups_into_queues_and_city(self):
        state = create_state(start=START)
        cruise_terminal.apply_scenario(state, **FAST_CLEAR)
        advance(state, 600)  # 08:10 — first call berthed and disembarking.
        summary = snapshot(state)['cruise_terminal']
        first = summary['ships'][0]
        self.assertEqual(first['status'], 'disembarking')
        self.assertEqual(first['berth'], 1)
        self.assertGreater(first['disembarked_people'], 0)
        self.assertLessEqual(first['disembarked_people'], first['modeled'])
        self.assertGreater(summary['ashore_people'], 0)
        self.assertGreater(summary['totals']['groups_created'], 0)
        for destination in summary['destinations']:
            self.assertLessEqual(destination['occupancy_people'], destination['capacity'])
        port_x, port_y = cruise_terminal.PORT_XY
        for group in state['cruise_terminal']['groups']:
            self.assertTrue(math.isfinite(group['x']) and math.isfinite(group['y']))
            self.assertLessEqual(math.hypot(group['x'] - port_x, group['y'] - port_y), 6000)
        states = {group['state'] for group in state['cruise_terminal']['groups']}
        self.assertTrue(states <= {'customs_queue', 'taxi_queue', 'shuttle_queue', 'transit',
                                   'at_destination', 'overflow_wait', 'security_queue'})

    def test_departure_conserves_passengers_and_boards_most(self):
        state = create_state(start=START)
        cruise_terminal.apply_scenario(state, **FAST_CLEAR)
        original_dwell = {identifier: destination['dwell_minutes']
                          for identifier, destination in cruise_terminal.DESTINATIONS.items()}
        for destination in cruise_terminal.DESTINATIONS.values():
            destination['dwell_minutes'] = (5, 15)  # Authored schedule compression for a fast test.
        try:
            advance(state, 600)  # 08:10 — groups are out from the terminal.
            visit = state['cruise_terminal']['visits'][0]
            # Compress the remaining schedule: everyone ashore quickly, shore
            # leave and boarding squeezed into minutes instead of hours.
            visit['duration_seconds'] = 300
            visit['disembark_end_ts'] = visit['arrival_ts'] + 300
            visit['boarding_start_ts'] = state['t'] + 1800  # 08:40 all-aboard call.
            visit['departure_ts'] = state['t'] + 2400  # 08:50 line-away.
            advance(state, 2500)  # Past the compressed departure.
        finally:
            for identifier, dwell in original_dwell.items():
                cruise_terminal.DESTINATIONS[identifier]['dwell_minutes'] = dwell
        summary = snapshot(state)['cruise_terminal']
        first = summary['ships'][0]
        self.assertEqual(first['status'], 'departed')
        self.assertEqual(first['boarded_people'] + first['missed_people'], first['disembarked_people'])
        self.assertGreater(first['boarded_people'], 0)
        self.assertGreater(first['boarded_people'], first['missed_people'])
        self.assertGreater(first['return_rate_pct'], 60)
        self.assertGreater(summary['totals']['spend'], 0)
        self.assertTrue(any(destination['visitors'] > 0 for destination in summary['destinations']))

    def test_single_berth_scenario_holds_the_second_ship(self):
        state = create_state(start=START)
        cruise_terminal.apply_scenario(state, berths=1)
        advance(state, 60)  # 08:01 — first call not yet arrived.
        second = state['cruise_terminal']['visits'][1]
        second['arrival_ts'] = state['t'] + 90  # Second call slips to just after the first.
        advance(state, 120)  # 08:03 — first call berths, second finds no free berth.
        # Key by call id: the same ship also has a call on the next day's timetable.
        statuses = {ship['id']: ship['status'] for ship in snapshot(state)['cruise_terminal']['ships']}
        self.assertEqual(statuses['2026-07-18:cruise0'], 'disembarking')
        self.assertEqual(statuses['2026-07-18:cruise1'], 'holding')

    def test_reduced_customs_capacity_backs_up_the_queue(self):
        fast = create_state(start=START)
        cruise_terminal.apply_scenario(fast, customs_lanes=12)
        slow = create_state(start=START)
        cruise_terminal.apply_scenario(slow, customs_lanes=1)
        advance(fast, 300)
        advance(slow, 300)
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

    def test_summary_reports_throughput_all_aboard_and_insights(self):
        state = create_state(start=START)
        advance(state, 600)
        summary = snapshot(state)['cruise_terminal']
        throughput = summary['throughput']
        self.assertEqual(set(throughput), {'disembarked_per_hour', 'boarded_per_hour', 'window_minutes'})
        self.assertTrue(all(value is None or value >= 0 for value in throughput.values()))
        self.assertIsNotNone(summary['all_aboard'])
        self.assertEqual(summary['all_aboard']['ship'], 'MV Aurora Atlantic')
        self.assertGreaterEqual(summary['all_aboard']['minutes_to_departure'], 0)
        self.assertGreaterEqual(summary['all_aboard']['minutes_to_boarding_start'], 0)
        self.assertLessEqual(len(summary['insights']), 8)
        for insight in summary['insights']:
            self.assertIn(insight['severity'], ('info', 'watch', 'alert'))
            self.assertTrue(insight['title'] and insight['detail'])

    def test_all_aboard_alert_fires_while_boarding_with_ashore_passengers(self):
        state = create_state(start=START)
        advance(state, 600)
        # Force the boarding window open early to exercise the advisory path.
        state['cruise_terminal']['visits'][0]['boarding_start_ts'] = state['t'] - 60
        state['cruise_terminal']['visits'][0]['status'] = 'boarding'
        titles = ' '.join(insight['title'] for insight in snapshot(state)['cruise_terminal']['insights'])
        self.assertIn('All aboard', titles)

    def test_json_restart_preserves_terminal_state_exactly(self):
        state = create_state(seed=7, start=START)
        advance(state, 120)
        restored = json.loads(json.dumps(state))
        advance(state, 300)
        advance(restored, 300)
        self.assertEqual(state['cruise_terminal'], restored['cruise_terminal'])
        self.assertEqual(snapshot(state)['cruise_terminal'], snapshot(restored)['cruise_terminal'])

    def test_terminal_layer_never_consumes_the_main_rng_stream(self):
        with_terminal = create_state(seed=11, start=START)
        advance(with_terminal, 120)
        # The engine people sample must be unaffected by terminal group activity:
        # same seed and start reproduce identical engine state in a run where the
        # terminal is disabled by pruning its sub-state before advancing.
        reference = create_state(seed=11, start=START)
        del reference['cruise_terminal']
        advance(reference, 120)
        self.assertEqual(with_terminal['people'], reference['people'])
        self.assertEqual(with_terminal['vehicles'], reference['vehicles'])
        self.assertEqual(with_terminal['rng_state'], reference['rng_state'])


class CruiseTerminalSceneTests(unittest.TestCase):
    def test_scene_contract_fields_and_bounds(self):
        scene = snapshot(create_state(start=START))['scene']
        self.assertEqual(scene['contract'], 'northstar.preview.scene.v1')
        self.assertEqual(scene['units'], 'm')
        self.assertTrue(scene['synthetic'])
        self.assertIn('illustrative', scene['geometry_note'])
        self.assertIn('northstar-local-m-v1', scene['geometry_note'])
        objects = scene['objects']
        # Halls are furnished rooms of the terminal interior, not separate objects.
        self.assertGreaterEqual(len(objects), 11)
        self.assertLessEqual(len(objects), 40)
        identifiers = [entry['id'] for entry in objects]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        for entry in objects:
            self.assertTrue({'id', 'label', 'kind', 'position', 'size', 'details'} <= set(entry))
            self.assertIn(entry['kind'], ('building', 'storage', 'dock', 'vehicle', 'vessel'))
            self.assertEqual(len(entry['position']), 2)
            self.assertEqual(len(entry['size']), 3)
            rows = entry['details']
            self.assertIsInstance(rows, list)
            for row in rows:
                self.assertTrue({'label', 'value'} <= set(row))
                self.assertIsInstance(row['label'], str) and self.assertTrue(row['label'])
        if 'model' in entry:
            self.assertRegex(entry['model'], r'^[a-z0-9][a-z0-9_-]*$')
        identifiers = {entry['id'] for entry in objects}
        for required in ('cruise_terminal_building', 'cruise_wharf',
                         'cruise_destination_harbour_market', 'cruise_destination_marina',
                         'cruise_destination_coastal_beach'):
            self.assertIn(required, identifiers)
        terminal = next(entry for entry in objects if entry['id'] == 'cruise_terminal_building')
        rooms = {room['label'] for room in terminal['interior']['rooms']}
        for hall in ('Check-in & Reception Hall', 'Customs & Immigration Hall', 'Security Screening',
                     'Waiting Lounge & Gates', 'Café, Retail & Duty-free'):
            self.assertIn(hall, rooms)
        self.assertGreaterEqual(len(scene['roads']), 6)
        for road in scene['roads']:
            self.assertIn('id', road)
            self.assertGreaterEqual(len(road['points']), 2)
            for point in road['points']:
                self.assertEqual(len(point), 2)

    def test_scene_destinations_sit_at_real_anchors_and_routes_follow_streets(self):
        from app.catalog import PLACE_INDEX, road_path
        state = create_state(start=START)
        advance(state, 600)
        scene = snapshot(state)['scene']
        by_id = {entry['id']: entry for entry in scene['objects']}
        for destination_id in cruise_terminal.DESTINATIONS:
            place = PLACE_INDEX[destination_id]
            entry = by_id[f'cruise_destination_{destination_id}']
            self.assertEqual(entry['position'], [place['x'], place['y']])
            self.assertTrue(any(row['label'] == 'Capacity visitors' for row in entry['details']))
            corridor = next(road for road in scene['roads'] if road['id'] == f'cruise_route_{destination_id}')
            path = road_path(cruise_terminal.PORT_PLACE, destination_id)['points']
            self.assertEqual(corridor['points'][0], list(path[0]))
            self.assertEqual(corridor['points'][-1], list(path[-1]))
            for point in corridor['points']:
                self.assertIn(point, [list(node) for node in path])

    def test_scene_ships_follow_modeled_berth_state(self):
        state = create_state(start=START)
        scene = snapshot(state)['scene']
        self.assertFalse([entry for entry in scene['objects'] if entry['id'].startswith('cruise_ship')])
        advance(state, 600)  # 08:10 — first call berthed.
        scene = snapshot(state)['scene']
        ships = [entry for entry in scene['objects']
                 if entry['kind'] == 'vessel' and entry['id'].startswith('cruise_ship')]
        self.assertEqual(len(ships), 1)
        ship = ships[0]
        self.assertEqual(ship['label'], 'MV Aurora Atlantic')
        self.assertEqual(ship['id'], 'cruise_ship_berth_1')
        self.assertEqual(ship['model'], 'cruise_ship')
        self.assertIn('heading', ship)
        rows = {row['label']: row['value'] for row in ship['details']}
        self.assertEqual(rows['Status'], 'Disembarking')
        self.assertEqual(rows['Berth'], 1)
        self.assertGreater(ship['position'][0], 26870)  # East of the wharf edge, in the water.
        # A holding call is shown offshore, not at a berth.
        state['cruise_terminal']['visits'][1]['status'] = 'holding'
        scene = snapshot(state)['scene']
        holding = [entry for entry in scene['objects'] if entry['id'] == 'cruise_ship_holding']
        self.assertEqual(len(holding), 1)
        self.assertEqual({row['label']: row['value'] for row in holding[0]['details']}['Status'],
                         'Holding outside the harbour')

    def test_scene_is_json_safe_and_deterministic(self):
        state = create_state(seed=3, start=START)
        advance(state, 300)
        scene = snapshot(state)['scene']
        self.assertEqual(json.loads(json.dumps(scene)), scene)
        self.assertEqual(snapshot(state)['scene'], scene)


if __name__ == '__main__':
    unittest.main()
