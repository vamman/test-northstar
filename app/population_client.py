"""Read-only client for the shared population state published by the clock coordinator.

Vendored unchanged into each simulator as app/population_client.py. The
coordinator writes public/population.json at every simulated day rollover and
simulators mount it read-only beside clock.json. A missing or malformed state
means population dynamics have not started for this run: callers fall back to
the static world population rather than failing.
"""

import json
import os
from pathlib import Path

CONTRACT = 'northstar.population.v1'


def path():
    """Location of the published population state (read-only mount)."""
    return Path(os.getenv('NORTHSTAR_POPULATION_FILE', '/northstar-clock/population.json'))


def read():
    """Return the validated population state, or None when unavailable."""
    try:
        data = json.loads(path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('contract') != CONTRACT:
        return None
    return data


def _world_population():
    """Static regional population from the vendored world file, if importable."""
    try:
        from app.world import WORLD
        return WORLD['population']
    except Exception:
        return None


def total(state=None):
    """Current regional population; falls back to the static world value."""
    state = state if state is not None else read()
    if state and state.get('population') is not None:
        return state['population']
    return _world_population()


def district_totals(state=None):
    """Current population per district id; empty when unavailable."""
    state = state if state is not None else read()
    if not state:
        return {}
    cohorts = state.get('districts', {})
    bands = cohorts[next(iter(cohorts))] if cohorts else {}
    return {district_id: sum(band[sex] for band in district.values() for sex in ('M', 'F'))
            for district_id, district in cohorts.items()}


def events(state=None, days=30):
    """Most recent daily birth/death events, oldest first."""
    state = state if state is not None else read()
    recorded = state.get('events', {}) if state else {}
    return [recorded[day] | {'day': day} for day in sorted(recorded)[-days:]]


def summary(state=None, event_days=7):
    """Compact view for dashboards and read APIs."""
    state = state if state is not None else read()
    if not state:
        population = _world_population()
        return {'available': False, 'population': population, 'last_day': None,
                'districts': {}, 'events': []}
    return {'available': True, 'population': state.get('population'), 'last_day': state.get('last_day'),
            'as_of': state.get('as_of'), 'epoch': state.get('epoch'),
            'districts': district_totals(state), 'events': events(state, event_days)}


def catchment(state=None):
    """Regional catchment as an int; falls back to the static world value."""
    population = total(state)
    return None if population is None else int(round(population))


def _static_world():
    """The world dict from the vendored module, or the vendored JSON beside this file."""
    try:
        from app.world import WORLD
        return WORLD
    except Exception:
        pass
    try:
        return json.loads(Path(__file__).with_name('northstar-world.json').read_text())
    except Exception:
        return None


def district_weights(districts, state=None):
    """Current per-district population weights, aligned with the given district list.

    Falls back to each district's static world population when dynamics are
    unavailable, so demand weighting keeps working before the first release.
    """
    state = state if state is not None else read()
    totals = district_totals(state) if state else {}
    fallback = _world_population() or 0
    weights = []
    for district in districts:
        dynamic = totals.get(district['id'])
        weights.append(dynamic if dynamic is not None else district.get('population', fallback))
    return weights


def population_ratio(state=None, baseline=200000):
    """Current regional population divided by the baseline; 1.0 when unavailable."""
    population = total(state)
    if not population:
        return 1.0
    return population / baseline


def band_share(state=None, band='0-19'):
    """Share of the regional population in one age band; None when unavailable."""
    state = state if state is not None else read()
    if not state:
        return None
    districts = state.get('districts', {})
    if not districts:
        return None
    in_band = sum(cohort[band][sex] for cohort in districts.values() if band in cohort
                  for sex in ('M', 'F'))
    grand_total = sum(population_of_cohort(cohort)
                      for district in districts.values() for cohort in district.values())
    return in_band / grand_total if grand_total else None


def population_of_cohort(cohort):
    """Total population across both sexes of one band's cohort dict."""
    return sum(cohort[sex] for sex in ('M', 'F'))


def affluent_youth_index(state=None, band='0-19'):
    """Youth population weighted by district affluence, relative to the world baseline.

    Drives demand that follows affluent young residents (university intake):
    richer districts' youth cohorts grow with in-migration-free natural increase,
    and this index rises or falls with them. Returns 1.0 when unavailable.
    """
    state = state if state is not None else read()
    world = _static_world()
    if not state or not world:
        return 1.0
    share = world.get('demographics', {}).get('age_distribution', {}).get(band)
    if not share:
        return 1.0
    current, baseline = 0.0, 0.0
    for district in world['districts']:
        affluence = 1.0 - district['deprivation']
        cohort = state.get('districts', {}).get(district['id'], {}).get(band, {})
        current += affluence * sum(cohort.get(sex, 0.0) for sex in ('M', 'F'))
        baseline += affluence * district['population'] * share
    return current / baseline if baseline else 1.0
