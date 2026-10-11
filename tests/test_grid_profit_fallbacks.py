import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("audit_all", ROOT / "src" / "audit_all.py")
audit_all = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_all)


class GridProfitFallbackTests(unittest.TestCase):
    def base_item(self, symbol="PUMPUSDT", roi=0, min_investment=166.790624):
        return {
            "strategyId": "test",
            "_category": "Spot Grid",
            "symbol": symbol,
            "roi": roi,
            "pnl": 0.01675845 if symbol == "PUMPUSDT" else 13.80068489,
            "minInvestment": min_investment,
            "matchedTrades": 66 if symbol == "PUMPUSDT" else 4750,
            "currentPrice": 0.005459 if symbol == "PUMPUSDT" else 13147464,
            "strategyParams": {
                "lowerLimit": 0.0035 if symbol == "PUMPUSDT" else 11000000,
                "upperLimit": 0.018 if symbol == "PUMPUSDT" else 15000000,
                "gridCount": 20 if symbol == "PUMPUSDT" else 100,
                "type": "GEOMETRIC" if symbol == "PUMPUSDT" else "ARITHMETIC",
            },
        }

    def test_jpy_quote_conversion_uses_binance_cross_rate(self):
        rate = audit_all._quote_usd_price(
            "BTCJPY", {"BTCUSDT": 83000.0, "BTCJPY": 13147464.0}
        )
        self.assertIsNotNone(rate)
        self.assertGreater(rate, 0)
        self.assertAlmostEqual(rate, 83000.0 / 13147464.0)

    def test_zero_roi_uses_minimum_investment_as_low_confidence_proxy(self):
        item = self.base_item()
        est = audit_all.estimate_grid_profit(item, item["pnl"], {})
        self.assertIsNotNone(est)
        self.assertIn("MIN_INVESTMENT_FALLBACK", est["method"])
        self.assertEqual(est["confidence"], "LOW")
        self.assertLessEqual(est["low"], est["mid"])
        self.assertLessEqual(est["mid"], est["high"])

    def test_positive_roi_jpy_bot_can_be_estimated_with_cross_rate(self):
        item = self.base_item("BTCJPY", roi=7.8, min_investment=0)
        prices = {"BTCUSDT": 83000.0, "BTCJPY": 13147464.0}
        est = audit_all.estimate_grid_profit(item, item["pnl"], prices)
        self.assertIsNotNone(est)
        self.assertLessEqual(est["low"], est["mid"])
        self.assertLessEqual(est["mid"], est["high"])

    def test_xrp_quote_conversion_uses_xrpusdt(self):
        rate = audit_all._quote_usd_price("TRXXRP", {"XRPUSDT": 0.52})
        self.assertAlmostEqual(rate, 0.52)

    def test_trxxrp_bots_can_be_estimated(self):
        item = {
            "_category": "Spot Grid", "symbol": "TRXXRP", "roi": 1.64,
            "pnl": 23.00074418, "minInvestment": 1392.363141,
            "matchedTrades": 312, "currentPrice": 0.2373,
            "strategyParams": {"lowerLimit": 0.16, "upperLimit": 0.34,
                               "gridCount": 67, "type": "GEOMETRIC"},
        }
        est = audit_all.estimate_grid_profit(item, item["pnl"], {"XRPUSDT": 0.52})
        self.assertIsNotNone(est)
        self.assertLessEqual(est["low"], est["mid"])
        self.assertLessEqual(est["mid"], est["high"])

    def test_marketplace_quote_suffixes_are_recognized(self):
        self.assertEqual(audit_all._quote_asset("BTCUSD1"), "USD1")
        self.assertEqual(audit_all._quote_asset("BTCU"), "U")
        self.assertAlmostEqual(audit_all._quote_usd_price("BTCUSD1", {}), 1.0)
        self.assertAlmostEqual(audit_all._quote_usd_price("BTCU", {}), 1.0)


if __name__ == "__main__":
    unittest.main()
