from copy import deepcopy
import pytest
from app import engine
from app.environment_client import neutral


def test_environment_slows_actual_travel_and_new_demand_and_recovers():
    original = engine.create_state()
    person = next(p for p in original['people'] if p['status'] == 'moving' and p['mode'] == 'walk'
                  and engine._path(p['origin_id'], p['destination_id'])[1][-1] - p['distance'] > 100)
    baseline, adverse = deepcopy(original), deepcopy(original)
    observation = neutral('transit', 'connected')
    observation.update(revision=82)
    observation['multipliers'].update(travel_time=2., demand=.5)
    engine.advance(baseline, 10)
    engine.advance(adverse, 10, environment=observation)
    normal_person = next(p for p in baseline['people'] if p['id'] == person['id'])
    slow_person = next(p for p in adverse['people'] if p['id'] == person['id'])
    assert slow_person['distance'] - person['distance'] == pytest.approx((normal_person['distance'] - person['distance']) / 2)
    assert adverse['road_traffic']['requested_vehicle_journeys'] <= baseline['road_traffic']['requested_vehicle_journeys']
    before = slow_person['distance']
    engine.advance(adverse, 10, environment=neutral('transit', 'unavailable'))
    recovered = next(p for p in adverse['people'] if p['id'] == person['id'])
    assert recovered['distance'] - before == pytest.approx(normal_person['distance'] - person['distance'])
    assert adverse['environment']['status'] == 'unavailable'
    assert adverse['environment']['applied_simulated_at'] == engine._iso(adverse['t'])
