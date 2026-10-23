import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import build_grid_setup as gs  # noqa: E402

HOUR = 3600.0


def series(values):
    values = np.asarray(values, dtype=float)
    return np.arange(len(values), dtype=float) * HOUR, values


class RecommendSetupTests(unittest.TestCase):
    def test_columns_always_match_setup_columns(self):
        times, prices = series([1.0] * 5)                                     # trop peu de points
        self.assertEqual(len(gs.recommend_setup(times, prices, 1.0)), len(gs.SETUP_COLUMNS))
        times = np.array([0.0] * 14 + [30 * 86400.0])                           # 14 anciens points, 1 seul récent
        prices = np.linspace(1.0, 1.1, 15)
        with mock.patch.object(gs, "HISTORY_DAYS", 11):                         # fenêtre glissante : 1 point récent seulement
            result = gs.recommend_setup(times, prices, 1.1)
        self.assertEqual(len(result), len(gs.SETUP_COLUMNS))
        self.assertEqual(result[-1], "INSUFFICIENT_HISTORY")

    def test_full_history_is_used_by_default(self):
        self.assertIsNone(gs.HISTORY_DAYS)
        times = np.arange(400, dtype=float) * 86400.0 / 4                       # 100 jours de points
        prices = np.concatenate([[50.0], np.full(399, 100.0)])                  # un plus bas très ancien
        low, high, *_ = gs.recommend_setup(times, prices, 100.0)
        self.assertAlmostEqual(low, 50.0 * (1 - gs.MARGIN))

    def test_range_has_margin_around_observed_extremes(self):
        times, prices = series(100 + 5 * np.sin(np.linspace(0, 6 * np.pi, 200)))
        low, high, count, mode, position, z, signal = gs.recommend_setup(times, prices, float(prices[-1]))
        self.assertAlmostEqual(low, float(prices.min()) * (1 - gs.MARGIN))
        self.assertAlmostEqual(high, float(prices.max()) * (1 + gs.MARGIN))
        self.assertLess(low, float(prices[-1]))
        self.assertGreater(high, float(prices[-1]))
        self.assertEqual((count, mode), (gs.GRID_COUNT, gs.GRID_MODE))

    def test_wait_when_price_is_high_in_72h_range(self):
        times, prices = series(np.linspace(100, 120, 120) + np.sin(np.arange(120)))
        *_, position, z, signal = gs.recommend_setup(times, prices, float(prices[-1]))
        self.assertGreater(position, 80)
        self.assertEqual(signal, "WAIT_HIGH_RANGE")

    def test_ready_when_price_is_mid_range(self):
        times, prices = series(100 + 5 * np.sin(np.linspace(0, 6 * np.pi, 193)))   # finit à 100 (milieu)
        *_, position, z, signal = gs.recommend_setup(times, prices, float(prices[-1]))
        self.assertTrue(20 <= position <= 80)
        self.assertEqual(signal, "READY_TO_REVIEW")


if __name__ == "__main__":
    unittest.main()
