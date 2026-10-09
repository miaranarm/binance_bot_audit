#!/usr/bin/env python3
"""src/validate_final_csv.py -- contrôle du fichier final results/current_multicriteria.csv
(29 colonnes demandées, produit par src/build_multicriteria.py).

Usage : python src/validate_final_csv.py [chemin.csv]
"""
import csv
import math
import sys
from pathlib import Path

CSV_PATH = Path(sys.argv[1] if len(sys.argv) > 1 else "results/current_multicriteria.csv")
EXPECTED = [
    "rank", "strategyId", "category", "strategyType", "symbol", "leverage",
    "minInvestment", "runningTime", "runningTime en J",
    "roi (fourni par Binance)", "roi (calculé)", "pnl", "matchedTrades",
    "Trades / J", "mdd7d", "gridProfit", "gridMode", "gridCount",
    "qtyPerOrderEstimate", "gridProfitEstimateLow", "gridProfitEstimateMid",
    "gridProfitEstimateHigh", "totalProfit", "gridProfitTotalProfitRatio",
    "floatingProfit", "currentPrice", "priceRange", "profitPerGridAfterFees",
    "score",
]
TOL = 1e-6


def numeric(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def fail(message):
    raise SystemExit("ERROR: " + message)


def main():
    if not CSV_PATH.exists() or CSV_PATH.stat().st_size == 0:
        fail(f"{CSV_PATH} missing or empty")
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != EXPECTED:
            fail(f"CSV schema mismatch\nExpected: {EXPECTED}\nActual: {reader.fieldnames}")
        rows = list(reader)
    if not rows:
        fail("no bots in final CSV")

    ids = [str(r.get("strategyId", "")).strip() for r in rows]
    if any(not s for s in ids):
        fail("empty strategyId")
    if len(ids) != len(set(ids)):
        fail("duplicate strategyId")
    if [numeric(r.get("rank")) for r in rows] != list(range(1, len(rows) + 1)):
        fail("rank is not contiguous from 1")

    scores = [numeric(r.get("score")) for r in rows]
    previous = None
    for r, score in zip(rows, scores):
        sid = r.get("strategyId")
        lev = numeric(r.get("leverage"))
        if lev is None or lev > 1:
            fail(f"invalid leverage for {sid}: {r.get('leverage')}")
        if score is not None:
            if not 0 <= score <= 100:
                fail(f"score out of 0-100 for {sid}: {score}")
            if previous is not None and score > previous + TOL:
                fail(f"ranking not sorted by score at {sid}")
            previous = score
        elif previous is None and any(s is not None for s in scores):
            fail(f"unscored bot ranked above scored bots: {sid}")

        runtime, days = numeric(r.get("runningTime")), numeric(r.get("runningTime en J"))
        if runtime is not None and days is not None and abs(days - runtime / 86400) > 1e-7:
            fail(f"runtime/day mismatch for {sid}")
        trades, per_day = numeric(r.get("matchedTrades")), numeric(r.get("Trades / J"))
        if runtime and runtime > 0 and trades is not None and per_day is not None:
            if abs(per_day - trades / (runtime / 86400)) > max(TOL, abs(per_day) * TOL):
                fail(f"Trades / J mismatch for {sid}")

        low, mid, high = (numeric(r.get(k)) for k in
                          ("gridProfitEstimateLow", "gridProfitEstimateMid", "gridProfitEstimateHigh"))
        if any(v is not None and v < 0 for v in (low, mid, high)):
            fail(f"negative Grid Profit estimate for {sid}")
        if None not in (low, mid, high) and not (low <= mid + TOL and mid <= high + TOL):
            fail(f"Grid Profit interval unordered for {sid}")

        grid = numeric(r.get("gridProfit"))      # renseigné = valeur exacte Binance
        if grid is not None and None not in (low, mid, high):
            if any(abs(v - grid) > max(TOL, abs(grid) * TOL) for v in (low, mid, high)):
                fail(f"exact Grid Profit differs from its interval for {sid}")

        ratio = numeric(r.get("gridProfitTotalProfitRatio"))
        if ratio is not None and not 0 <= ratio <= 5:
            fail(f"ratio out of range for {sid}: {ratio}")

    exact = sum(numeric(r.get("gridProfit")) is not None for r in rows)
    scored = sum(s is not None for s in scores)
    print(f"PASS: {len(rows)} unique bots, leverage <= 1, {len(EXPECTED)} columns, "
          f"{scored} scored, {exact} exact Grid Profit.")


if __name__ == "__main__":
    main()
