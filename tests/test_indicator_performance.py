import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))
import analyze_indicator_performance as ap  # noqa: E402


class LookAheadTests(unittest.TestCase):
    def test_last_row_never_uses_the_future(self):
        idx = pd.date_range("2026-10-01", periods=10, freq="1h")
        f = pd.DataFrame({"adx14": np.arange(10.0)}, index=idx)
        row = ap.last_row(f, idx[4] + pd.Timedelta(minutes=30), ["adx14"])
        self.assertEqual(row["adx14"], 4.0)                      # barre de 04:00, pas celle de 05:00
        self.assertTrue(np.isnan(ap.last_row(f, idx[0] - pd.Timedelta(hours=1), ["adx14"])["adx14"]))
        self.assertTrue(np.isnan(ap.last_row(f, idx[-1] + pd.Timedelta(hours=6), ["adx14"])["adx14"]))


class StatsTests(unittest.TestCase):
    def test_spearman_monotone_and_binary_target(self):
        x = pd.Series(np.arange(60.0))
        self.assertAlmostEqual(ap.spearman(x, x ** 2), 1.0)
        self.assertGreater(ap.spearman(x, (x > 30).astype(float)), 0.8)
        self.assertTrue(np.isnan(ap.spearman(x[:10], x[:10])))   # trop peu d'observations

    def test_indicator_table_detects_planted_signal(self):
        rng = np.random.default_rng(0)
        parts = []
        for k in range(12):
            f = rng.normal(size=200)
            parts.append(pd.DataFrame({"slice": pd.Timestamp("2026-10-01") + pd.Timedelta(days=k),
                                       **{c: rng.normal(size=200) for c in ap.FEATURES},
                                       **{t: rng.normal(size=200) for t in ap.TARGETS},
                                       "adx14": f, "pnlPct24h": f + rng.normal(0, 1, 200),
                                       "exitedRange": (rng.random(200) < 0.1).astype(float)}))
        table = ap.indicator_table(pd.concat(parts, ignore_index=True), "TEST")
        row = table[(table.feature == "adx14") & (table.target == "pnlPct24h")].iloc[0]
        self.assertEqual(row["signal"], "SIGNIFICANT")
        self.assertGreater(row["icMean"], 0.5)
        self.assertGreater(row["highTercileMedian"], row["lowTercileMedian"])
        noise = table[(table.feature == "rsi14") & (table.target == "pnlPct24h")].iloc[0]
        self.assertNotEqual(noise["signal"], "SIGNIFICANT")

    def test_few_slices_are_flagged_insufficient(self):
        self.assertEqual(ap.signal_label(3, 5.0), "INSUFFICIENT")
        self.assertEqual(ap.signal_label(12, 2.5), "SIGNIFICANT")
        self.assertEqual(ap.signal_label(12, 0.4), "NOT_SIGNIFICANT")


if __name__ == "__main__":
    unittest.main()
