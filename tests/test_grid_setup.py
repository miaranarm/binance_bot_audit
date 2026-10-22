import unittest

from src.build_grid_setup import evaluate_rule, simulate_grid


class GridSetupTests(unittest.TestCase):
    def test_grid_replay_counts_completed_cycles_and_nets_fees(self):
        prices = [0.95, 0.80, 0.95, 1.10, 0.95, 0.80, 1.10]
        result = simulate_grid(prices, 0.80, 1.20, 4)
        self.assertIsNotNone(result)
        self.assertGreater(result["cycles"], 0)
        self.assertGreater(result["profit_pct"], 0)
        self.assertGreaterEqual(result["outside_pct"], 0)
        self.assertLessEqual(result["outside_pct"], 100)

    def test_rule_uses_prior_range_and_returns_outside_measure(self):
        window = {
            "low72": 0.90,
            "high72": 1.10,
            "future": [1.00, 0.95, 1.05, 1.00],
        }
        result = evaluate_rule(window, 0.04, 6)
        self.assertAlmostEqual(result["low"], 0.864)
        self.assertAlmostEqual(result["high"], 1.144)
        self.assertGreaterEqual(result["outside_pct"], 0)
        self.assertLessEqual(result["outside_pct"], 100)

    def test_invalid_range_is_rejected(self):
        self.assertIsNone(simulate_grid([1.0, 1.1], 0.0, 1.0, 6))


if __name__ == "__main__":
    unittest.main()
