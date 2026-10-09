import csv
import json
from pathlib import Path

CSV = Path("results/current_multicriteria.csv")
OUT = Path("results/grid_profit_validation.json")
TOL = 2e-5

def f(row, key):
    try:
        return float(row.get(key, ""))
    except (TypeError, ValueError):
        return None

rows = list(csv.DictReader(CSV.open(newline="", encoding="utf-8")))
stats = {
    "rows": len(rows), "reconstructed": 0, "exact": 0, "unavailable": 0,
    "unknownSource": 0, "orderingErrors": 0, "ratioErrors": 0,
    "floatingErrors": 0, "sourceErrors": 0, "negativeEstimateRows": 0,
    "ratiosOver100Percent": 0, "estimatedRatiosOver100Percent": 0,
}
examples = []
def add_example(payload):
    if len(examples) < 10:
        examples.append(payload)

for row in rows:
    source = row.get("gridProfitSource", "").strip()
    ratio_source = row.get("gridProfitTotalProfitRatioSource", "").strip()
    ratio_status = row.get("gridProfitTotalProfitRatioStatus", "").strip()
    grid = f(row, "gridProfit")
    low, mid, high = f(row, "gridProfitEstimateLow"), f(row, "gridProfitEstimateMid"), f(row, "gridProfitEstimateHigh")
    total, ratio, floating = f(row, "totalProfit"), f(row, "gridProfitTotalProfitRatio"), f(row, "floatingProfit")
    rlow, rmid, rhigh = f(row, "gridProfitTotalProfitRatioEstimateLow"), f(row, "gridProfitTotalProfitRatioEstimateMid"), f(row, "gridProfitTotalProfitRatioEstimateHigh")

    if source == "RECONSTRUCTED":
        stats["reconstructed"] += 1
        # At the scanner stage the midpoint is still present in gridProfit;
        # the finalizer moves it to the estimate columns and blanks the headline
        # Grid Profit and floatingProfit fields for non-exact rows.
        if grid is None or floating is not None:
            stats["sourceErrors"] += 1
            add_example({"strategyId": row.get("strategyId"), "type": "BAD_RECONSTRUCTED_METRIC_STATE", "gridProfit": grid, "floatingProfit": floating})
        if ratio_status != "ESTIMATED_NOT_EXACT" or ratio_source != "RECONSTRUCTED_GRID_PROFIT_DIV_BINANCE_MARKETPLACE_TOTAL_PROFIT":
            stats["sourceErrors"] += 1
            add_example({"strategyId": row.get("strategyId"), "type": "BAD_RECONSTRUCTED_RATIO_PROVENANCE", "status": ratio_status, "source": ratio_source})
        if ratio is None:
            stats["sourceErrors"] += 1
            add_example({"strategyId": row.get("strategyId"), "type": "MISSING_PRIMARY_ESTIMATED_RATIO"})
        if rmid is not None and ratio is not None and abs(ratio-rmid) > 1e-6:
            stats["ratioErrors"] += 1
            add_example({"strategyId": row.get("strategyId"), "type": "PRIMARY_RATIO_MISMATCH", "ratio": ratio, "mid": rmid})
        if any(v is not None and v < 0 for v in (low, mid, high)):
            # Negative reconstructed estimates may be economically valid when
            # estimated profit per grid after fees is negative. Preserve and report them.
            stats["negativeEstimateRows"] += 1
            add_example({
                "strategyId": row.get("strategyId"),
                "type": "NEGATIVE_GRID_PROFIT_ESTIMATE_RETAINED",
                "low": low, "mid": mid, "high": high,
                "profitPerGridAfterFees": row.get("profitPerGridAfterFees"),
            })
        if None in (low, mid, high) or not (low <= mid + TOL and mid <= high + TOL):
            stats["orderingErrors"] += 1
            add_example({"strategyId": row.get("strategyId"), "type": "GRID_ESTIMATE_ORDER", "low": low, "mid": mid, "high": high})
        if None not in (rlow, rmid, rhigh) and not (rlow <= rmid + TOL and rmid <= rhigh + TOL):
            stats["orderingErrors"] += 1
            add_example({"strategyId": row.get("strategyId"), "type": "RATIO_ESTIMATE_ORDER", "low": rlow, "mid": rmid, "high": rhigh})
        if rmid is not None and rmid > 1:
            stats["estimatedRatiosOver100Percent"] += 1

    elif source == "BINANCE_EXACT":
        stats["exact"] += 1
        if grid is None:
            stats["sourceErrors"] += 1
            add_example({"strategyId": row.get("strategyId"), "type": "EXACT_MISSING_GRIDPROFIT"})
        if None in (low, mid, high) or not all(abs(v-grid) <= TOL for v in (low, mid, high)):
            stats["sourceErrors"] += 1
            add_example({"strategyId": row.get("strategyId"), "type": "EXACT_ESTIMATE_MISMATCH"})
        if ratio_source not in {"BINANCE_DETAIL_GRID_DIV_TOTAL_EXACT", "BINANCE_DETAIL_TOTAL_MINUS_FLOATING_DIV_TOTAL_EXACT"} or ratio_status != "EXACT":
            stats["sourceErrors"] += 1
            add_example({"strategyId": row.get("strategyId"), "type": "BAD_EXACT_RATIO_PROVENANCE", "source": ratio_source, "status": ratio_status})
        if ratio is not None and ratio > 1:
            stats["ratiosOver100Percent"] += 1

    elif source in ("", "UNAVAILABLE"):
        stats["unavailable"] += 1
    else:
        stats["unknownSource"] += 1
        add_example({"strategyId": row.get("strategyId"), "type": "UNKNOWN_SOURCE", "source": source})

stats["status"] = "PASS" if not any(stats[k] for k in ("orderingErrors","ratioErrors","floatingErrors","sourceErrors","unknownSource")) else "FAIL"
OUT.write_text(json.dumps({"validation": stats, "examples": examples}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"validation": stats, "examples": examples}, ensure_ascii=False))
if stats["status"] == "FAIL":
    raise SystemExit(1)
