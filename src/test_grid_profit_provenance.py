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


audit_all, build, validate = load("audit_all"), load("build_multicriteria"), load("validate")


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
        futures = {"strategyId": "10", "_category": "Futures Grid", "_lev": 1.0, "symbol": "BTCUSDT", "pnl": 5.0,
                   "roi": "1.00", "runningTime": 86400, "matchedTrades": 4, "_detail_grid_profit": 3.0,
                   "_detail_total_profit": 4.0,
                   "strategyParams": {"lowerLimit": 50000, "upperLimit": 70000, "gridCount": 10, "type": "2"}}
        leveraged = dict(futures, strategyId="11", _lev=5.0)
        current = audit_all.build_current([spot, futures, leveraged], {"XRPUSDT": 0.5})
        self.assertEqual([r["strategyId"] for r in current], ["9", "10"])
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
            self.assertTrue(rows[0]["gridProfitTotalProfitRatio"].startswith("≈"))
            self.assertEqual(rows[0]["gridProfit"], "")
            self.assertEqual(rows[0]["floatingProfit"], "")
            self.assertEqual(float(rows[1]["gridProfitTotalProfitRatio"]), 0.75)
            self.assertEqual(float(rows[1]["floatingProfit"]), 1.0)
            self.assertTrue(rows[0]["profitPerGridAfterFees"])
            self.assertEqual((rows[0]["rank"], rows[0]["score"]), ("", ""))
            self.assertEqual(len(rows[0]), 29)
            summary = (Path(tmp) / "current_multicriteria_summary.txt").read_text(encoding="utf-8")
            self.assertGreater(summary.count("\n"), 2)                            # vrais retours à la ligne


if __name__ == "__main__":
    unittest.main()
