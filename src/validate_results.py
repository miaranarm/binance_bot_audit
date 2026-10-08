import csv
import json
from pathlib import Path

CSV = Path("results/current_multicriteria.csv")
OUT = Path("results/grid_profit_validation.json")
TOL = 2e-5

def f(row, key):
    value = row.get(key, "")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

rows = list(csv.DictReader(CSV.open(newline="", encoding="utf-8")))
stats = {
    "rows": len(rows),
    "reconstructed": 0,
    "exact": 0,
    "unavailable": 0,
    "orderingErrors": 0,
    "ratioErrors": 0,
    "floatingErrors": 0,
    "invalidNegativeEstimate": 0,
    "ratiosOver100Percent": 0,
}
examples = []

for row in rows:
    source = row.get("gridProfitSource", "")
    if source == "RECONSTRUCTED":
        stats["reconstructed"] += 1
        low, mid, high = f(row, "gridProfitEstimateLow"), f(row, "gridProfitEstimateMid"), f(row, "gridProfitEstimateHigh")
        total, ratio, floating = f(row, "totalProfit"), f(row, "gridProfitTotalProfitRatio"), f(row, "floatingProfit")
        if any(v is not None and v < 0 for v in (low, mid, high)):
            stats["invalidNegativeEstimate"] += 1
        if None in (low, mid, high) or not (low <= mid + TOL and mid <= high + TOL):
            stats["orderingErrors"] += 1
            if len(examples) < 10:
                examples.append({"strategyId": row.get("strategyId"), "type": "ORDER", "low": low, "mid": mid, "high": high})
        if total not in (None, 0) and ratio is not None and mid is not None and abs(ratio - mid / total) > TOL:
            stats["ratioErrors"] += 1
            if len(examples) < 10:
                examples.append({"strategyId": row.get("strategyId"), "type": "RATIO", "ratio": ratio, "expected": mid / total})
        if total is not None and mid is not None and floating is not None:
            if abs(floating - (total - mid)) > 1e-7 * max(1.0, abs(total)):
                stats["floatingErrors"] += 1
                if len(examples) < 10:
                    examples.append({"strategyId": row.get("strategyId"), "type": "FLOATING", "floating": floating, "expected": total - mid})
        if ratio is not None and ratio > 1:
            stats["ratiosOver100Percent"] += 1
    elif source == "BINANCE_EXACT":
        stats["exact"] += 1
        if row.get("gridProfitEstimateLow") not in ("", None) and f(row, "gridProfitEstimateLow") is None:
            stats["orderingErrors"] += 1
    elif not source:
        stats["unavailable"] += 1

stats["status"] = "PASS" if not any(stats[k] for k in ("orderingErrors", "ratioErrors", "floatingErrors", "invalidNegativeEstimate")) else "FAIL"
result = {"validation": stats, "examples": examples}
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
if stats["status"] == "FAIL":
    raise SystemExit(1)
