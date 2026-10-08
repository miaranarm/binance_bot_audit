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
    "unknownSource": 0,
    "orderingErrors": 0,
    "ratioErrors": 0,
    "floatingErrors": 0,
    "sourceErrors": 0,
    "invalidNegativeEstimate": 0,
    "ratiosOver100Percent": 0,
}
examples = []


def add_example(payload):
    if len(examples) < 10:
        examples.append(payload)


for row in rows:
    source = row.get("gridProfitSource", "").strip()
    ratio_source = row.get("gridProfitTotalProfitRatioSource", "").strip()
    grid = f(row, "gridProfit")
    low, mid, high = (
        f(row, "gridProfitEstimateLow"),
        f(row, "gridProfitEstimateMid"),
        f(row, "gridProfitEstimateHigh"),
    )
    total, ratio, floating = (
        f(row, "totalProfit"),
        f(row, "gridProfitTotalProfitRatio"),
        f(row, "floatingProfit"),
    )

    if source == "RECONSTRUCTED":
        stats["reconstructed"] += 1

        # A reconstructed row must never masquerade as an exact Binance value.
        if grid is not None:
            stats["sourceErrors"] += 1
            add_example({"strategyId": row.get("strategyId"), "type": "RECONSTRUCTED_HAS_EXACT_GRIDPROFIT", "gridProfit": grid})

        if ratio_source != "RECONSTRUCTED_GRID_PROFIT_DIV_BINANCE_MARKETPLACE_TOTAL_PROFIT":
            stats["sourceErrors"] += 1
            add_example({"strategyId": row.get("strategyId"), "type": "BAD_RECONSTRUCTED_RATIO_SOURCE", "source": ratio_source})

        if any(v is not None and v < 0 for v in (low, mid, high)):
            stats["invalidNegativeEstimate"] += 1

        if None in (low, mid, high) or not (low <= mid + TOL and mid <= high + TOL):
            stats["orderingErrors"] += 1
            add_example({"strategyId": row.get("strategyId"), "type": "ORDER", "low": low, "mid": mid, "high": high})

        if total not in (None, 0) and ratio is not None and mid is not None and abs(ratio - mid / total) > TOL:
            stats["ratioErrors"] += 1
            add_example({"strategyId": row.get("strategyId"), "type": "RATIO", "ratio": ratio, "expected": mid / total})

        if total is not None and mid is not None and floating is not None:
            if abs(floating - (total - mid)) > 1e-7 * max(1.0, abs(total)):
                stats["floatingErrors"] += 1
                add_example({"strategyId": row.get("strategyId"), "type": "FLOATING", "floating": floating, "expected": total - mid})

        if ratio is not None and ratio > 1:
            stats["ratiosOver100Percent"] += 1

    elif source == "BINANCE_EXACT":
        stats["exact"] += 1

        if grid is None:
            stats["sourceErrors"] += 1
            add_example({"strategyId": row.get("strategyId"), "type": "EXACT_MISSING_GRIDPROFIT"})

        if low is None or mid is None or high is None or not (
            abs(low - grid) <= TOL and abs(mid - grid) <= TOL and abs(high - grid) <= TOL
        ):
            stats["sourceErrors"] += 1
            add_example({"strategyId": row.get("strategyId"), "type": "EXACT_ESTIMATE_MISMATCH"})

        if ratio_source not in {
            "BINANCE_DETAIL_GRID_DIV_TOTAL_EXACT",
            "BINANCE_DETAIL_TOTAL_MINUS_FLOATING_DIV_TOTAL_EXACT",
        }:
            stats["sourceErrors"] += 1
            add_example({"strategyId": row.get("strategyId"), "type": "BAD_EXACT_RATIO_SOURCE", "source": ratio_source})

    elif source in ("", "UNAVAILABLE"):
        stats["unavailable"] += 1

    else:
        stats["unknownSource"] += 1
        add_example({"strategyId": row.get("strategyId"), "type": "UNKNOWN_SOURCE", "source": source})


stats["status"] = (
    "PASS"
    if not any(
        stats[k]
        for k in (
            "orderingErrors",
            "ratioErrors",
            "floatingErrors",
            "sourceErrors",
            "unknownSource",
            "invalidNegativeEstimate",
        )
    )
    else "FAIL"
)

result = {"validation": stats, "examples": examples}
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))

if stats["status"] == "FAIL":
    raise SystemExit(1)
