"""Administrative read-only scheduling projection."""
from .adaptive_policy import intervals_for, policy_for
from .monitoring_capacity import capacity_snapshot


def monitoring_projection(camera_id, state, latest, now):
    capacity = capacity_snapshot()
    if state is None:
        return None, capacity
    policy = policy_for(camera_id)
    leased = bool(state['lease_until'] and state['lease_until'] > now)
    delay = max(0, (now - state['next_analysis_at']).total_seconds())
    if leased:
        scheduling = 'RUNNING' if state['lease_started_at'] else 'QUEUED'
    elif delay > 0 and state['capacity_deferred_at']:
        scheduling = 'WAITING_CAPACITY'
    else:
        scheduling = 'DUE' if delay > 0 else 'SCHEDULED'
    context = latest.get('context', {}) if latest else {}
    trend = context.get('trend') if context.get('policy_version') == policy else None
    state.update(policy_version=policy, interval_seconds=intervals_for(policy)[state['level']],
                 delay_seconds=round(delay, 1), scheduling_status=scheduling,
                 trend=trend or {'status': 'INSUFFICIENT_DATA', 'values': [], 'rising': False})
    return state, capacity
