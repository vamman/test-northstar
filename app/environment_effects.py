"""Persist evidence of environmental factors actually applied by an engine step."""
from copy import deepcopy
from .environment_client import neutral, factor


def applied(peer, observation, clock, effect, keys):
    result = deepcopy(observation) if observation is not None else {**neutral(peer, 'not_observed'), 'observed_at': None}
    result.update(applied=True, applied_simulated_at=clock, effect=effect,
                  applied_multipliers={key: factor(result, key) for key in keys})
    return result


def multiplier(payload, key):
    return factor(payload.get('environment') or {'multipliers': {}}, key)


def pending(peer):
    return {**neutral(peer, 'not_applied'), 'applied': False,
            'applied_simulated_at': None, 'applied_multipliers': {},
            'effect': 'No environment observation has been applied by this simulator yet.'}


def report(stored, peer, paused=False, heartbeat=None, max_age=180):
    """Expose persisted evidence with current liveness, without applying any factor."""
    from datetime import datetime, timezone
    result = deepcopy(stored or pending(peer))
    result['observation_status'] = result['status']
    result['last_applied'] = bool(result.get('applied'))
    now = datetime.now(timezone.utc)
    def parsed(value):
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
    if paused:
        result['status'] = 'paused'
    elif result.get('applied'):
        try:
            if not heartbeat or (now - parsed(heartbeat)).total_seconds() > max_age:
                result['status'] = 'worker_stale'
            elif result['status'] == 'connected' and parsed(result['valid_until']) <= now:
                result['status'] = 'stale'
        except (TypeError, KeyError, ValueError):
            result['status'] = 'stale'
    result['applied'] = bool(result.get('applied') and result['status'] == 'connected')
    result['application_status'] = 'active' if result['applied'] else 'historical' if result['last_applied'] else 'not_applied'
    result['checked_at'] = now.isoformat()
    return result
