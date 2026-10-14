"""Pure, deterministic camera monitoring policy (all probabilities are percentages)."""
from dataclasses import dataclass
import math

from django.conf import settings


INTERVALS = {'NORMAL': 300, 'WATCH': 60, 'CRITICAL': 30, 'RECOVERY': 120}


@dataclass(frozen=True)
class Decision:
    level: str
    strong_streak: int
    clear_streak: int
    reason: str


def decide(level, strong_streak, clear_streak, classification, *, contextual=False, rising=False):
    if classification not in {'NO_INDICATION', 'INTERMEDIATE_INDICATION', 'FLOOD_INDICATION'}:
        return Decision(level, 0, 0, 'ANALYSIS_UNAVAILABLE')
    strong = strong_streak + 1 if classification == 'FLOOD_INDICATION' else 0
    clear = clear_streak + 1 if classification == 'NO_INDICATION' and not contextual and not rising else 0
    if strong >= 3:
        return Decision('CRITICAL', strong, 0, 'REPEATED_STRONG')
    if strong or classification == 'INTERMEDIATE_INDICATION' or contextual or rising:
        return Decision('CRITICAL' if level == 'CRITICAL' else 'WATCH', strong, 0, 'INDICATION_OR_CONTEXT')
    if level == 'CRITICAL':
        return Decision('RECOVERY' if clear >= 5 else level, strong, clear, 'CLEAR_OBSERVATIONS')
    if level == 'WATCH':
        return Decision('RECOVERY' if clear >= 5 else level, strong, clear, 'CLEAR_OBSERVATIONS')
    if level == 'RECOVERY' and clear >= 10:
        return Decision('NORMAL', strong, clear, 'STABLE_RECOVERY')
    return Decision(level, strong, clear, 'MONITORING')


CONSERVATIVE_INTERVALS = {'NORMAL': 300, 'WATCH': 120, 'CRITICAL': 60, 'RECOVERY': 180}


def policy_for(camera_id):
    selected = getattr(settings, 'FLOOD_MONITORING_POLICY', 'legacy')
    pilot = getattr(settings, 'FLOOD_MONITORING_PILOT_CAMERAS', ())
    if selected == 'conservative' and (not pilot or str(camera_id) in pilot):
        return 'conservative:' + getattr(settings, 'FLOOD_MONITORING_POLICY_EPOCH', 'v1')
    return 'legacy'


def intervals_for(policy):
    return CONSERVATIVE_INTERVALS if policy.startswith('conservative:') else INTERVALS


def mean_probability(frames):
    values = [frame.get('flooded') for frame in frames]
    if not values or any(isinstance(v, bool) or not isinstance(v, (int, float))
                         or not math.isfinite(v) or not 0 <= v <= 100 for v in values):
        return None
    return sum(values) / len(values)


def trend_window(previous, current, *, policy):
    """Up to three consecutive observations, oldest first; never skip failures."""
    window = []
    for item in [*previous, current]:
        if not item['valid'] or not item['model_version'] or item['policy'] != policy:
            window = []
            continue
        if window and (item['model_version'] != window[-1]['model_version']
                       or not 0 < (item['started_at'] - window[-1]['started_at']).total_seconds()
                       <= getattr(settings, 'FLOOD_MONITORING_MAX_GAP_SECONDS', 600)):
            window = []
        window.append(item)
    values = [item['probability'] for item in window[-3:]]
    delta = getattr(settings, 'FLOOD_MONITORING_TREND_DELTA', 5.0)
    rising = len(values) == 3 and all(b - a >= delta for a, b in zip(values, values[1:]))
    return {'values': values, 'rising': rising,
            'status': 'RISING' if rising else 'STABLE' if len(values) == 3 else 'INSUFFICIENT_DATA'}


def decide_conservative(level, strong_streak, clear_streak, classification, *, rising=False):
    if classification not in {'NO_INDICATION', 'INTERMEDIATE_INDICATION', 'FLOOD_INDICATION'}:
        return Decision(level, 0, 0, 'ANALYSIS_UNAVAILABLE')
    strong = strong_streak + 1 if classification == 'FLOOD_INDICATION' else 0
    clear = clear_streak + 1 if classification == 'NO_INDICATION' and not rising else 0
    if strong >= 3:
        return Decision('CRITICAL', strong, 0, 'REPEATED_STRONG')
    if strong or rising:
        return Decision('CRITICAL' if level == 'CRITICAL' else 'WATCH', strong, 0,
                        'STRONG_INDICATION' if strong else 'SUSTAINED_GROWTH')
    if level in {'WATCH', 'CRITICAL'} and clear >= 5:
        return Decision('RECOVERY', 0, 0, 'CLEAR_OBSERVATIONS')
    if level == 'RECOVERY' and clear >= 5:
        return Decision('NORMAL', 0, 0, 'STABLE_RECOVERY')
    return Decision(level, strong, clear if level != 'NORMAL' else 0, 'MONITORING')
