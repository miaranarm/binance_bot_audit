import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))
import build_bot_indicators as bi  # noqa: E402


def frame(values):
    idx = pd.date_range("2026-10-01", periods=len(values), freq="1h")
    s = pd.Series(values, index=idx)
    return pd.DataFrame({"high": s, "low": s, "close": s})


class RegimeTests(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(1)

    def label(self, values):
        r = bi.symbol_indicators(frame(values), "1h")
        return bi.regime(r["adx14"], r["efficiencyRatio48"])

    def test_trend_range_and_random_walk(self):
        trend = 100 * np.exp(np.cumsum(np.random.default_rng(2).normal(0.004, 0.002, 400)))
        rng_ = 100 + 2 * np.sin(np.arange(400) / 2) + np.random.default_rng(3).normal(0, 0.3, 400)
        walk = 100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.005, 400)))
        self.assertEqual(self.label(trend), "TREND")
        self.assertEqual(self.label(rng_), "RANGE")
        self.assertEqual(self.label(walk), "MIXED")

    def test_chop_only_counts_when_given(self):
        self.assertEqual(bi.regime(22, 0.2), "MIXED")
        self.assertEqual(bi.regime(22, 0.2, 70), "MIXED")   # 1 vote range seulement
        self.assertEqual(bi.regime(15, 0.2, 70), "RANGE")


class RelativeRegimeTests(unittest.TestCase):
    def table(self, n, tf="1h"):
        k = np.linspace(0, 1, n)
        return pd.DataFrame({"indicatorTimeframe": tf, "adx14": 10 + 40 * k,
                             "efficiencyRatio48": 0.02 + 0.4 * k, "chop14": 70 - 40 * k})

    def test_terciles_per_timeframe(self):
        t = pd.concat([self.table(30, "1h"), self.table(30, "4h")], ignore_index=True)
        label, score = bi.relative_regimes(t)
        for tf in ("1h", "4h"):
            sub = label[t["indicatorTimeframe"] == tf]
            self.assertEqual(sub.value_counts().to_dict(), {"RANGE": 10, "MIXED": 10, "TREND": 10})
        self.assertEqual(label.iloc[0], "RANGE")
        self.assertEqual(label.iloc[29], "TREND")
        self.assertGreater(score.iloc[29], score.iloc[0])

    def test_missing_chop_uses_remaining_indicators(self):
        t = self.table(30)
        t["chop14"] = np.nan
        label, _ = bi.relative_regimes(t)
        self.assertEqual((label.iloc[0], label.iloc[29]), ("RANGE", "TREND"))

    def test_small_group_falls_back_to_absolute_rule(self):
        t = pd.DataFrame({"indicatorTimeframe": ["1h"] * 3, "adx14": [15, 40, 22],
                          "efficiencyRatio48": [0.1, 0.4, 0.2], "chop14": [70, 30, 50]})
        label, _ = bi.relative_regimes(t)
        self.assertEqual(label.tolist(), ["RANGE", "TREND", "MIXED"])


class GeometryTests(unittest.TestCase):
    def test_grid_step_and_exit_risk(self):
        row = {"priceRangeLow": 100.0, "priceRangeHigh": 200.0, "gridCount": 10,
               "currentPrice": 150.0, "gridMode": "GEOMETRIC"}
        self.assertAlmostEqual(bi.grid_step_pct(row), 100 * (2 ** 0.1 - 1))
        row["gridMode"] = "ARITHMETIC"
        self.assertAlmostEqual(bi.grid_step_pct(row), 100 * 10 / 150)
        near = bi.exit_risk(101, 100, 200, 0.03)
        far = bi.exit_risk(150, 100, 200, 0.03)
        self.assertGreater(near, far)
        self.assertTrue(np.isnan(bi.exit_risk(90, 100, 200, 0.03)))  # prix hors fourchette

    def test_parse_klines_drops_open_bar_and_short_series(self):
        rows = [[i * 3600000, "1", "2", "0.5", "1.5", "10"] + [0] * 6 for i in range(100)]
        out = bi.parse_klines(rows)
        self.assertEqual(len(out), 99)
        self.assertIsNone(bi.parse_klines(rows[:10]))
        self.assertIsNone(bi.parse_klines({"code": -1121}))


if __name__ == "__main__":
    unittest.main()
