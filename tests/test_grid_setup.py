import unittest
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from src.build_grid_setup import add_setup_columns, evaluate_rule, simulate_grid


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

    def test_setup_columns_can_hold_numeric_and_text_values(self):
        times = np.arange(200, dtype=float) * 3600.0
        prices = 1.0 + 0.1 * np.sin(np.arange(200, dtype=float) / 10.0)
        current = pd.DataFrame([{"symbol": "ABCUSDT", "currentPrice": 1.0, "priceLow": 0.8}])
        output = add_setup_columns(
            current,
            {"ABCUSDT": (times, prices)},
            {},
            {"bufferPct": 4.0, "gridCount": 6},
            datetime.now(timezone.utc),
        )
        self.assertGreater(float(output.loc[0, "recommendedPriceLow"]), 0)
        self.assertGreater(float(output.loc[0, "recommendedPriceHigh"]), float(output.loc[0, "recommendedPriceLow"]))
        self.assertEqual(output.loc[0, "recommendedGridCount"], 6)
        self.assertIn(output.loc[0, "setupSignal"], {"READY_TO_REVIEW", "WAIT_HIGH_RANGE", "WAIT_OUTSIDE_RANGE"})

    def test_arithmetic_and_geometric_levels_differ(self):
        from src.build_grid_setup import make_levels
        arithmetic = make_levels(1.0, 4.0, 3, "ARITHMETIC")
        geometric = make_levels(1.0, 4.0, 3, "GEOMETRIC")
        self.assertAlmostEqual(arithmetic[1], 2.0)
        self.assertNotAlmostEqual(geometric[1], arithmetic[1])

    def test_initial_setup_profit_uses_only_past_data_to_configure(self):
        from src.build_grid_setup import historical_initial_setup_profit
        times = np.arange(24 * 12, dtype=float) * 3600.0
        prices = 1.0 + 0.08 * np.sin(np.arange(len(times), dtype=float) / 7.0)
        results = historical_initial_setup_profit({"ABCUSDT": (times, prices)})
        self.assertIn("ABCUSDT", results)
        self.assertTrue(np.isfinite(results["ABCUSDT"]))

    def test_invalid_range_is_rejected(self):
        self.assertIsNone(simulate_grid([1.0, 1.1], 0.0, 1.0, 6))


if __name__ == "__main__":
    unittest.main()
