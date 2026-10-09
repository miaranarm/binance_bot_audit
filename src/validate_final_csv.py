#!/usr/bin/env python3
"""Validate the public-facing current_multicriteria.csv contract."""
import csv
import math
from pathlib import Path

CSV_PATH = Path("results/current_multicriteria.csv")
EXPECTED = [
    "rank", "strategyId", "category", "strategyType", "symbol", "leverage",
    "minInvestment", "runningTime", "runningTime en J",
    "roi (fourni par Binance)", "roi (calculé)", "pnl", "matchedTrades",
    "Trades / J", "mdd7d", "gridProfit", "gridProfitSource", "gridProfitEstimateStatus",
    "gridMode", "gridCount", "qtyPerOrderEstimate", "gridProfitEstimateLow",
    "gridProfitEstimateMid", "gridProfitEstimateHigh", "totalProfit",
    "gridProfitTotalProfitRatio", "floatingProfit", "floatingProfitSource",
    "currentPrice", "priceRange", "profitPerGridAfterFees", "score",
]

def numeric(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None

def main():
    if not CSV_PATH.exists() or CSV_PATH.stat().st_size == 0:
        raise SystemExit(f"ERROR: {CSV_PATH} missing or empty")
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != EXPECTED:
            raise SystemExit(
                "ERROR: CSV schema mismatch\\n"
                f"Expected: {EXPECTED}\\nActual: {reader.fieldnames}"
            )
        rows = list(reader)
    if not rows:
        raise SystemExit("ERROR: no bots in final CSV")

    ids = [str(row.get("strategyId", "")).strip() for row in rows]
    if any(not sid for sid in ids):
        raise SystemExit("ERROR: empty strategyId")
    if len(ids) != len(set(ids)):
        raise SystemExit("ERROR: duplicate strategyId")
    ranks = [numeric(row.get("rank")) for row in rows]
    if ranks != list(range(1, len(rows) + 1)):
        raise SystemExit("ERROR: rank is not contiguous from 1")
    for row in rows:
        lev = numeric(row.get("leverage"))
        if lev is None or lev > 1:
            raise SystemExit(f"ERROR: invalid leverage for {row.get('strategyId')}: {row.get('leverage')}")
        runtime = numeric(row.get("runningTime"))
        days = numeric(row.get("runningTime en J"))
        if runtime is not None and days is not None and abs(days - runtime / 86400) > 1e-7:
            raise SystemExit(f"ERROR: runtime/day mismatch for {row.get('strategyId')}")
        trades = numeric(row.get("matchedTrades"))
        trades_day = numeric(row.get("Trades / J"))
        if runtime and runtime > 0 and trades is not None and trades_day is not None:
            if abs(trades_day - trades / (runtime / 86400)) > max(1e-6, abs(trades_day) * 1e-6):
                raise SystemExit(f"ERROR: Trades / J mismatch for {row.get('strategyId')}")
        low = numeric(row.get("gridProfitEstimateLow"))
        mid = numeric(row.get("gridProfitEstimateMid"))
        high = numeric(row.get("gridProfitEstimateHigh"))
        if None not in (low, mid, high) and not (low <= mid + 1e-8 and mid <= high + 1e-8):
            raise SystemExit(f"ERROR: Grid Profit interval unordered for {row.get('strategyId')}")
        gp = numeric(row.get("gridProfit"))
        status = row.get("gridProfitEstimateStatus")
        if status == "ESTIMATED_NOT_EXACT" and mid is not None and gp is not None and abs(gp-mid) > max(1e-6, abs(mid)*1e-6):
            raise SystemExit(f"ERROR: reconstructed Grid Profit not aligned with midpoint for {row.get('strategyId')}")
        if status == "EXACT" and gp is None:
            raise SystemExit(f"ERROR: exact Grid Profit missing for {row.get('strategyId')}")
        if status == "ESTIMATED_NOT_EXACT" and row.get("gridProfitSource") != "RECONSTRUCTED":
            raise SystemExit(f"ERROR: estimated Grid Profit source mismatch for {row.get('strategyId')}")
    print(f"PASS: {len(rows)} unique bots, leverage <= 1, {len(EXPECTED)} columns; Grid Profit provenance checked.")

if __name__ == "__main__":
    main()
