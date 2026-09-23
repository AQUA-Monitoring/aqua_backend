from unittest import TestCase
from core.flood_camera_monitoring.services.adaptive_policy import decide


class AdaptivePolicyTests(TestCase):
    def test_strong_requires_three_successive_observations(self):
        first = decide('NORMAL', 0, 0, 'FLOOD_INDICATION')
        second = decide(first.level, first.strong_streak, 0, 'FLOOD_INDICATION')
        third = decide(second.level, second.strong_streak, 0, 'FLOOD_INDICATION')
        self.assertEqual([first.level, second.level, third.level], ['WATCH', 'WATCH', 'CRITICAL'])

    def test_terminal_false_negative_with_neighbor_stays_under_watch(self):
        self.assertEqual(decide('NORMAL', 0, 0, 'NO_INDICATION', contextual=True).level, 'WATCH')

    def test_one_clear_frame_cannot_end_crisis(self):
        self.assertEqual(decide('CRITICAL', 3, 0, 'NO_INDICATION').level, 'CRITICAL')
        self.assertEqual(decide('CRITICAL', 0, 4, 'NO_INDICATION').level, 'RECOVERY')
        self.assertEqual(decide('RECOVERY', 0, 9, 'NO_INDICATION').level, 'NORMAL')

    def test_failure_resets_streak_without_downgrading(self):
        decision = decide('CRITICAL', 3, 4, None)
        self.assertEqual((decision.level, decision.strong_streak, decision.clear_streak), ('CRITICAL', 0, 0))
