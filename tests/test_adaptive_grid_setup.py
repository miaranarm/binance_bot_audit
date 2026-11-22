import unittest
import numpy as np

from src.build_grid_setup import learn_lower_multiplier, recommend_setup


class AdaptiveGridLearningTests(unittest.TestCase):
    def test_learning_waits_until_fourteen_days(self):
        timestamps = np.arange(10 * 24, dtype=float) * 3600
        prices = 100 + np.sin(np.arange(len(timestamps)) / 8)
        result = learn_lower_multiplier(timestamps, prices)
        self.assertFalse(result["active"])
        self.assertEqual(result["reason"], "LESS_THAN_14_DAYS")

    def test_learning_reports_completed_walk_forward_episodes(self):
        timestamps = np.arange(20 * 24, dtype=float) * 3600
        # Deterministic nonstationary series with both rebounds and new lows.
        x = np.arange(len(timestamps), dtype=float)
        prices = 100 * np.exp(-0.0004 * x + 0.018 * np.sin(x / 11))
        result = learn_lower_multiplier(timestamps, prices)
        self.assertGreaterEqual(result["episodes"], 5)
        self.assertIsNotNone(result["multiplier"])
        self.assertIn("target_met", result)
        self.assertIn(str(result["multiplier"]), result["candidate_rates"])

    def test_recommended_low_is_positive_and_high_is_widened(self):
        timestamps = np.arange(20 * 24, dtype=float) * 3600
        x = np.arange(len(timestamps), dtype=float)
        prices = 1.0 * np.exp(-0.0002 * x + 0.02 * np.sin(x / 10))
        values = recommend_setup(timestamps, prices, float(prices[-1]))
        self.assertGreater(values[0], 0)
        self.assertGreater(values[1], float(np.max(prices)))
        self.assertEqual(values[2], 5)
        self.assertEqual(values[3], "GEOMETRIC")


if __name__ == "__main__":
    unittest.main()
