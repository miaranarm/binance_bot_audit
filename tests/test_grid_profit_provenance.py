import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("audit_all", ROOT / "src" / "audit_all.py")
audit_all = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_all)


class GridProfitProvenanceTests(unittest.TestCase):
    def metric(self, item):
        result = audit_all.grid_metrics(item, {})
        return {
            "ratio": result[0],
            "ratio_source": result[4],
            "ratio_status": result[8],
            "grid_profit": result[9],
            "total_profit": result[10],
            "grid_source": result[11],
            "total_source": result[15],
            "floating_profit": result[16],
            "floating_basis": result[17],
        }

    def test_spot_marketplace_total_does_not_get_detail_only_exact_ratio(self):
        row = self.metric({
            "strategyId": "spot-test",
            "_category": "Spot Grid",
            "pnl": 100.0,
            "_detail_grid_profit": 30.0,
            "_detail_total_profit": 40.0,
        })
        self.assertEqual(row["total_source"], "BINANCE_MARKETPLACE_PNL_USD_AS_TOTAL_PROFIT")
        self.assertNotEqual(row["ratio_status"], "EXACT")
        self.assertNotEqual(row["grid_source"], "BINANCE_EXACT")
        self.assertIsNone(row["floating_profit"])

    def test_matching_detail_basis_keeps_exact_grid_ratio(self):
        row = self.metric({
            "strategyId": "detail-test",
            "_category": "Futures Grid",
            "_detail_grid_profit": 30.0,
            "_detail_total_profit": 40.0,
        })
        self.assertEqual(row["total_source"], "BINANCE_DETAIL_TOTAL_PROFIT")
        self.assertEqual(row["ratio_status"], "EXACT")
        self.assertEqual(row["ratio_source"], "BINANCE_DETAIL_GRID_DIV_TOTAL_EXACT")
        self.assertAlmostEqual(float(row["ratio"]), 0.75)
        self.assertEqual(row["grid_source"], "BINANCE_EXACT")
        self.assertAlmostEqual(row["grid_profit"], 30.0)

    def test_detail_total_minus_floating_uses_same_basis(self):
        row = self.metric({
            "strategyId": "floating-test",
            "_category": "Futures Grid",
            "_detail_total_profit": 40.0,
            "_detail_floating_pnl": 10.0,
        })
        self.assertEqual(row["ratio_status"], "EXACT")
        self.assertEqual(row["ratio_source"], "BINANCE_DETAIL_TOTAL_MINUS_FLOATING_DIV_TOTAL_EXACT")
        self.assertAlmostEqual(float(row["ratio"]), 0.75)
        self.assertAlmostEqual(row["grid_profit"], 30.0)
        self.assertAlmostEqual(row["floating_profit"], 10.0)
        self.assertEqual(row["floating_basis"], "TOTAL_MINUS_EXACT_GRID_PROFIT")


if __name__ == "__main__":
    unittest.main()
