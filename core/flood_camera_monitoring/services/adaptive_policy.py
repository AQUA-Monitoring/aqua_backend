"""Pure, deterministic camera monitoring policy (all probabilities are percentages)."""
from dataclasses import dataclass


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
