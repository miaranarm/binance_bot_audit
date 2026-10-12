import csv
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "src" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_all, build, validate, select = load("audit_all"), load("build_multicriteria"), load("validate"), load("select_bots")


class GridProfitProvenanceTests(unittest.TestCase):
    def test_spot_without_known_quote_keeps_marketplace_total_and_no_exact(self):
        m = audit_all.grid_metrics({"strategyId": "s", "_category": "Spot Grid", "pnl": 100.0,
                                    "_detail_grid_profit": 30.0, "_detail_total_profit": 40.0}, {})
        self.assertEqual(m["totalSource"], "BINANCE_MARKETPLACE_PNL_USD_AS_TOTAL_PROFIT")
        self.assertNotEqual(m["ratioStatus"], "EXACT")
        self.assertNotEqual(m["gridSource"], "BINANCE_EXACT")
        self.assertIsNone(m["floating"])

    def test_spot_floating_detail_is_not_used_without_same_basis(self):
        m = audit_all.grid_metrics({"strategyId": "s", "_category": "Spot Grid", "pnl": 100.0,
                                    "_detail_total_profit": 40.0, "_detail_floating_pnl": 10.0}, {})
        self.assertNotEqual(m["ratioStatus"], "EXACT")
        self.assertIsNone(m["floating"])

    def test_spot_usdt_detail_consistent_with_pnl_gives_exact(self):
        m = audit_all.grid_metrics({"strategyId": "s", "_category": "Spot Grid", "symbol": "XRPUSDT", "pnl": 38.0,
                                    "_detail_grid_profit": 30.0, "_detail_total_profit": 40.0}, {})
        self.assertEqual(m["totalSource"], "BINANCE_DETAIL_TOTAL_PROFIT")
        self.assertEqual(m["gridSource"], "BINANCE_EXACT")
        self.assertEqual(m["ratioStatus"], "EXACT")
        self.assertAlmostEqual(float(m["ratio"]), 0.75)
        self.assertAlmostEqual(m["floating"], 10.0)

    def test_spot_usdt_detail_with_other_unit_is_rejected(self):
        m = audit_all.grid_metrics({"strategyId": "s", "_category": "Spot Grid", "symbol": "XRPUSDT", "pnl": 500.0,
                                    "_detail_grid_profit": 0.3, "_detail_total_profit": 0.4}, {})
        self.assertEqual(m["totalSource"], "BINANCE_MARKETPLACE_PNL_USD_AS_TOTAL_PROFIT")
        self.assertNotEqual(m["gridSource"], "BINANCE_EXACT")

    def test_spot_btc_quote_is_never_exact(self):
        m = audit_all.grid_metrics({"strategyId": "s", "_category": "Spot Grid", "symbol": "XRPBTC", "pnl": 40.0,
                                    "_detail_grid_profit": 30.0, "_detail_total_profit": 40.0}, {})
        self.assertNotEqual(m["gridSource"], "BINANCE_EXACT")

    def test_futures_detail_keeps_exact_grid_ratio(self):
        m = audit_all.grid_metrics({"strategyId": "f", "_category": "Futures Grid",
                                    "_detail_grid_profit": 30.0, "_detail_total_profit": 40.0}, {})
        self.assertEqual(m["totalSource"], "BINANCE_DETAIL_TOTAL_PROFIT")
        self.assertEqual(m["ratioSource"], "BINANCE_DETAIL_GRID_DIV_TOTAL_EXACT")
        self.assertAlmostEqual(float(m["ratio"]), 0.75)
        self.assertAlmostEqual(m["gridProfit"], 30.0)

    def test_futures_total_minus_floating_same_basis(self):
        m = audit_all.grid_metrics({"strategyId": "f", "_category": "Futures Grid",
                                    "_detail_total_profit": 40.0, "_detail_floating_pnl": 10.0}, {})
        self.assertEqual(m["ratioSource"], "BINANCE_DETAIL_TOTAL_MINUS_FLOATING_DIV_TOTAL_EXACT")
        self.assertAlmostEqual(float(m["ratio"]), 0.75)
        self.assertAlmostEqual(m["gridProfit"], 30.0)
        self.assertAlmostEqual(m["floating"], 10.0)
        self.assertEqual(m["floatingBasis"], "TOTAL_MINUS_EXACT_GRID_PROFIT")


    def test_negative_roi_still_gets_an_estimate(self):
        item = {"strategyId": "n", "_category": "Spot Grid", "_lev": 1.0, "symbol": "ETHUSDT", "pnl": -12.0,
                "roi": "-1.20", "matchedTrades": 50,
                "strategyParams": {"lowerLimit": 2000, "upperLimit": 3000, "gridCount": 25, "type": "2"}}
        m = audit_all.grid_metrics(dict(item, _audit_current_price=2500.0), {})
        self.assertEqual(m["gridSource"], "RECONSTRUCTED")
        self.assertLessEqual(m["low"], m["mid"])
        self.assertLessEqual(m["mid"], m["high"])
        self.assertTrue(m["ratio"])
        self.assertLessEqual(m["ratioLow"], m["ratioMid"])      # Total Profit négatif : ordre des bornes conservé
        self.assertLessEqual(m["ratioMid"], m["ratioHigh"])
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw.csv"
            audit_all.write_csv(raw, audit_all.build_current([item], {"ETHUSDT": 2500.0}))
            stats, _ = validate.check_raw(raw)
            self.assertEqual((stats["status"], stats["reconstructed"]), ("PASS", 1))

    def test_roi_and_pnl_of_opposite_sign_give_no_estimate(self):
        item = {"strategyId": "n", "_category": "Spot Grid", "symbol": "ETHUSDT", "pnl": 12.0, "roi": "-1.20",
                "matchedTrades": 50, "_audit_current_price": 2500.0,
                "strategyParams": {"lowerLimit": 2000, "upperLimit": 3000, "gridCount": 25, "type": "2"}}
        self.assertEqual(audit_all.grid_metrics(item, {})["gridSource"], "")


    def test_price_out_of_range_is_still_estimated(self):
        for price in (1500.0, 3500.0):
            item = {"strategyId": "o", "_category": "Spot Grid", "symbol": "ETHUSDT", "pnl": 40.0, "roi": "4.00",
                    "matchedTrades": 50, "_audit_current_price": price,
                    "strategyParams": {"lowerLimit": 2000, "upperLimit": 3000, "gridCount": 25, "type": "1"}}
            m = audit_all.grid_metrics(item, {})
            self.assertEqual(m["gridSource"], "RECONSTRUCTED", price)
            self.assertLessEqual(m["low"], m["mid"])
            self.assertLessEqual(m["mid"], m["high"])
            self.assertEqual(m["confidence"], "LOW")
            self.assertTrue(m["method"].endswith("OUT_OF_RANGE_START_BOUNDED"))
            self.assertTrue(m["ratio"])

    def test_inside_range_estimate_is_unchanged_in_kind(self):
        item = {"strategyId": "i", "_category": "Spot Grid", "symbol": "ETHUSDT", "pnl": 40.0, "roi": "4.00",
                "matchedTrades": 50, "_audit_current_price": 2500.0,
                "strategyParams": {"lowerLimit": 2000, "upperLimit": 3000, "gridCount": 25, "type": "1"}}
        self.assertEqual(audit_all.grid_metrics(item, {})["confidence"], "MEDIUM")


class CollectionTests(unittest.TestCase):
    class FakePage:
        def __init__(self, pages_by_no):
            self.pages_by_no = pages_by_no
            self.request = self

        def post(self, endpoint, data, timeout):
            rows = self.pages_by_no.get(data["page"], [])
            fake = type("R", (), {"ok": True, "status": 200, "json": lambda s: {"data": rows}})()
            return fake

        def wait_for_timeout(self, ms):
            pass

    def test_empty_family_is_complete_not_a_warning(self):
        stats = {}
        rows = audit_all.pages(self.FakePage({}), "x", {}, "Marketplace type 15", "T", [], stats)
        self.assertEqual(rows, [])
        self.assertTrue(stats["Marketplace type 15"]["complete"])

    def test_pagination_continues_without_announced_total(self):
        full = lambda n: [{"strategyId": f"{n}-{i}", "symbol": "BTCUSDT"} for i in range(100)]
        stats = {}
        rows = audit_all.pages(self.FakePage({1: full(1), 2: full(2), 3: full(3)[:40]}), "x", {}, "Spot Grid", "S", [], stats)
        self.assertEqual(len(rows), 240)
        self.assertFalse(stats["Spot Grid"]["complete"])        # total inconnu : jamais déclaré complet


class ProfitPerGridTests(unittest.TestCase):
    def ppg(self, mode, **extra):
        item = {"strategyParams": {"lowerLimit": 100, "upperLimit": 200, "gridCount": 10, "type": mode}, **extra}
        return audit_all.grid_metrics(item, {})["profitPerGrid"]

    def test_geometric(self):
        self.assertEqual(self.ppg("2"), "6.9702%")

    def test_arithmetic_range(self):
        self.assertEqual(self.ppg("1"), "5.0579% - 9.7900%")

    def test_binance_value_wins(self):
        self.assertEqual(self.ppg("2", profitPerGrid=0.8), "0.8000%")

    def test_unknown_mode_is_blank(self):
        self.assertEqual(self.ppg(""), "")


class PipelineTests(unittest.TestCase):
    def test_raw_build_validate(self):
        spot = {"strategyId": "9", "_category": "Spot Grid", "_lev": 1.0, "symbol": "XRPUSDT", "pnl": 20.0,
                "roi": "2.00", "runningTime": 864000, "matchedTrades": 100, "minInvestment": 1000, "mdd7d": 3.0,
                "strategyParams": {"lowerLimit": 0.4, "upperLimit": 0.6, "gridCount": 20, "type": "1"}}
        spot_exact = dict(spot, strategyId="10", symbol="BTCUSDT", pnl=5.0, roi="1.00", runningTime=86400,
                          matchedTrades=4, minInvestment=500, _detail_grid_profit=3.0, _detail_total_profit=4.0,
                          strategyParams={"lowerLimit": 50000, "upperLimit": 70000, "gridCount": 10, "type": "2"})
        futures = dict(spot_exact, strategyId="11", _category="Futures Grid")
        current = audit_all.build_current([spot, spot_exact, futures], {"XRPUSDT": 0.5})
        self.assertEqual([r["strategyId"] for r in current], ["9", "10"])        # Spot Grid uniquement
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "current_multicriteria.csv"
            audit_all.write_csv(raw, current)
            stats, _ = validate.check_raw(raw)
            self.assertEqual(stats["status"], "PASS", stats)
            self.assertEqual((stats["reconstructed"], stats["exact"]), (1, 1))
            build.main(str(raw), str(raw))
            self.assertIn("PASS", validate.check_final(raw))
            with raw.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([r["strategyId"] for r in rows], ["9", "10"])      # tri numérique
            self.assertEqual(len(rows[0]), len(validate.FINAL_COLUMNS))          # schéma figé
            self.assertEqual(rows[0]["gridProfit"], "")                          # estimé : jamais présenté comme exact
            est_total = float(rows[0]["totalProfit"])
            est_mid = float(rows[0]["gridProfitEstimateMid"])
            self.assertAlmostEqual(float(rows[0]["floatingProfit"]), est_total - est_mid, places=4)
            self.assertAlmostEqual(float(rows[0]["gridProfitTotalProfitRatio"]), est_mid / est_total, places=5)
            self.assertEqual(float(rows[1]["gridProfit"]), 3.0)                  # exact conservé
            self.assertEqual(float(rows[1]["gridProfitTotalProfitRatio"]), 0.75)
            self.assertEqual(float(rows[1]["floatingProfit"]), 1.0)
            self.assertTrue(rows[0]["profitPerGridAfterFeesMid"])
            summary = (Path(tmp) / "current_multicriteria_summary.txt").read_text(encoding="utf-8")
            self.assertGreater(summary.count("\n"), 2)


class FidelityTests(unittest.TestCase):
    def test_floating_loss_cannot_exceed_open_sell_orders(self):
        # ROI arrondi très bas (0,05 %) => capital implicite énorme => Grid Profit absurde sans la borne physique.
        item = {"strategyId": "f", "_category": "Spot Grid", "symbol": "ARUSDT", "pnl": 282.0, "roi": "0.05",
                "matchedTrades": 4751, "minInvestment": 100, "_audit_current_price": 5.0,
                "strategyParams": {"lowerLimit": 2.0, "upperLimit": 8.0, "gridCount": 40, "type": "1"}}
        m = audit_all.grid_metrics(item, {})
        self.assertEqual(m["gridSource"], "RECONSTRUCTED")
        self.assertTrue(m["method"].endswith("FEASIBILITY_CLAMPED"))
        self.assertEqual(m["confidence"], "LOW")
        qty = m["qty"]
        open_loss = qty * sum(l - 5.0 for l in audit_all._grid_levels(2.0, 8.0, 40, "ARITHMETIC")[1:] if l > 5.0)
        self.assertGreaterEqual(m["totalProfit"] - m["mid"], -open_loss - 1e-6)

    def test_invested_capital_never_below_minimum_investment(self):
        item = {"strategyId": "m", "_category": "Spot Grid", "symbol": "ETHUSDT", "pnl": 40.0, "roi": "40.00",
                "matchedTrades": 50, "minInvestment": 1000, "_audit_current_price": 2500.0,
                "strategyParams": {"lowerLimit": 2000, "upperLimit": 3000, "gridCount": 25, "type": "1"}}
        low = audit_all.grid_metrics(dict(item, minInvestment=1.0), {})["qty"]
        high = audit_all.grid_metrics(item, {})["qty"]
        self.assertGreater(high, low)           # ROI 40 % => capital 100 ; plancher 1000 appliqué

    def test_geometric_niveau_uses_log_scale(self):
        import pandas as pd
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "c.csv"
            item = {"strategyId": "g", "_category": "Spot Grid", "_lev": 1.0, "symbol": "SOLUSDT", "pnl": 20.0, "roi": "2.00",
                    "runningTime": 864000, "matchedTrades": 100, "minInvestment": 1000, "_audit_current_price": 100.0,
                    "strategyParams": {"lowerLimit": 50, "upperLimit": 200, "gridCount": 20, "type": "2"}}
            audit_all.write_csv(raw, audit_all.build_current([item], {"SOLUSDT": 100.0}))
            build.main(str(raw), str(raw))
            self.assertAlmostEqual(float(pd.read_csv(raw)["Niveau"][0]), 0.0, places=6)   # 100 = moyenne géométrique de 50 et 200
            self.assertIn("PASS", validate.check_final(raw))


class SelectBotsTests(unittest.TestCase):
    def test_base_asset_groups_quotes(self):
        self.assertEqual({select.base_asset(s) for s in ("DOGEUSDT", "DOGEUSDC", "DOGEFDUSD")}, {"DOGE"})

    def test_recent_history_filters_dead_bots(self):
        import gzip
        import datetime as dt
        import pandas as pd
        ref = pd.read_csv(ROOT / "tests" / "fixture_final.csv", dtype={"strategyId": str})
        with tempfile.TemporaryDirectory() as tmp:
            final = Path(tmp) / "current_multicriteria.csv"
            ref.to_csv(final, index=False)
            now = dt.datetime(2026, 10, 12, 12, tzinfo=dt.timezone.utc)
            then = now - dt.timedelta(days=7)
            folder = Path(tmp) / "history" / then.strftime("%Y/%m/%d")
            folder.mkdir(parents=True)
            snap = pd.DataFrame({"strategyId": ref.strategyId, "roi": ref["roi (fourni par Binance)"] - 7 * 0.2,
                                 "matchedTrades": ref.matchedTrades - 7 * 10})
            dead = ref.strategyId.iloc[0]                         # bot sans aucun mouvement sur 7 jours
            snap.loc[snap.strategyId == dead, ["roi", "matchedTrades"]] = ref.loc[0, ["roi (fourni par Binance)", "matchedTrades"]].values
            with gzip.open(folder / f"{then.strftime('%Y%m%dT%H%M%SZ')}.csv.gz", "wt", encoding="utf-8") as h:
                snap.to_csv(h, index=False)
            out = select.main(str(final), 100.0, 1, now=now)
            self.assertNotIn(dead, set(out.strategyId))
            self.assertTrue(out["recent_daily_pct"].notna().all())
            self.assertTrue((out["recent_trades_day"] >= 1).all())
            self.assertTrue(out["rank"].is_monotonic_increasing and out["score"].is_monotonic_decreasing)
            self.assertEqual(out.base.nunique() if "base" in out else out.symbol.map(select.base_asset).nunique(), len(out))


class HistoryRetentionTests(unittest.TestCase):
    def test_old_snapshots_are_purged_and_recent_kept(self):
        import datetime as dt
        now = dt.datetime(2026, 10, 12, 12, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            old_history, old_out = audit_all.HISTORY, audit_all.OUT
            audit_all.OUT = Path(tmp)
            audit_all.HISTORY = Path(tmp) / "history"
            try:
                audit_all.write_history([], now - dt.timedelta(days=45))
                snapshot, removed = audit_all.write_history([], now)
                self.assertEqual(removed, 1)
                self.assertTrue(snapshot.exists())
                self.assertEqual(len(list(audit_all.HISTORY.rglob("*.csv.gz"))), 1)
            finally:
                audit_all.HISTORY, audit_all.OUT = old_history, old_out


if __name__ == "__main__":
    unittest.main()
