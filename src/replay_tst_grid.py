#!/usr/bin/env python3
"""Replay the TSTUSDT 5-grid geometric setup from every saved currentPrice snapshot.

This is an approximation, not an exchange-exact Binance bot replica. It uses the
repository's compressed snapshots, median currentPrice per snapshot, 0.1% fees
per side, and marks any unsold bought inventory to the final snapshot price.
"""
from datetime import datetime
from pathlib import Path
import csv
import gzip
import math
import re

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "results" / "history"
OUT = ROOT / "results"
SYMBOL = "TSTUSDT"
START_DATE = datetime(2026, 10, 11).date()
END_DATE = datetime(2026, 10, 22).date()
LOW = 0.0142
HIGH = 0.0166
GRID_COUNT = 5
BUDGET = 100.0
FEE = 0.001
TIME_RE = re.compile(r"^(\d{8}T\d{6})Z\.csv\.gz$")


def get_stamp(path):
    match = TIME_RE.match(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=None)
    except ValueError:
        return None


def load_prices():
    rows = []
    for path in sorted(HISTORY.rglob("*.csv.gz")):
        stamp = get_stamp(path)
        if stamp is None or not (START_DATE <= stamp.date() <= END_DATE):
            continue
        try:
            with gzip.open(path, "rt", newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                values = []
                for row in reader:
                    if str(row.get("symbol", "")).strip() != SYMBOL:
                        continue
                    try:
                        p = float(row.get("currentPrice", ""))
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(p) and p > 0:
                        values.append(p)
                if values:
                    rows.append((stamp.isoformat(sep=" "), float(np.median(values)), len(values)))
        except (OSError, UnicodeError, csv.Error):
            continue
    # Deduplicate timestamps; each snapshot contributes one median currentPrice.
    return sorted({r[0]: r for r in rows}.values(), key=lambda r: r[0])


def main():
    rows = load_prices()
    if len(rows) < 2:
        raise SystemExit(f"Insufficient TSTUSDT snapshots from {START_DATE} to {END_DATE}: {len(rows)}")

    levels = np.geomspace(LOW, HIGH, GRID_COUNT + 1)
    # Approximate grid-cycle replay: each cell can hold one buy lot at a time.
    # Each buy lot uses 1/5 of the 100 USDT reference budget.
    lot_qty = [BUDGET / GRID_COUNT / levels[i] for i in range(GRID_COUNT)]
    bought = [False] * GRID_COUNT
    buy_prices = [None] * GRID_COUNT
    realized_net = 0.0
    cycles = 0
    total_buy_cost = 0.0
    total_buy_fees = 0.0
    events = []
    previous = rows[0][1]
    start_price = previous
    events.append({
        "timestamp": rows[0][0], "currentPrice": previous, "event": "START",
        "grid_level": "", "realized_profit_USDT": 0.0,
        "open_inventory_value_USDT": 0.0, "cumulative_realized_USDT": 0.0,
    })

    for timestamp, current, source_rows in rows[1:]:
        if current < previous:
            crossed = [k for k in range(GRID_COUNT) if previous > levels[k] >= current]
            for k in reversed(crossed):
                if not bought[k]:
                    bought[k] = True
                    buy_prices[k] = float(levels[k])
                    total_buy_cost += lot_qty[k] * levels[k]
                    total_buy_fees += lot_qty[k] * levels[k] * FEE
                    events.append({
                        "timestamp": timestamp, "currentPrice": current, "event": "BUY",
                        "grid_level": f"{levels[k]:.10g}",
                        "realized_profit_USDT": -lot_qty[k] * levels[k] * FEE,
                        "open_inventory_value_USDT": lot_qty[k] * current,
                        "cumulative_realized_USDT": realized_net - total_buy_fees,
                    })
        elif current > previous:
            crossed = [k for k in range(1, GRID_COUNT + 1) if previous < levels[k] <= current]
            for k in crossed:
                cell = k - 1
                if bought[cell]:
                    sell_price = float(levels[cell + 1])
                    buy_price = float(buy_prices[cell])
                    gross = lot_qty[cell] * (sell_price - buy_price)
                    fees = FEE * lot_qty[cell] * (sell_price + buy_price)
                    net = gross - fees
                    realized_net += net
                    cycles += 1
                    bought[cell] = False
                    buy_prices[cell] = None
                    events.append({
                        "timestamp": timestamp, "currentPrice": current, "event": "SELL",
                        "grid_level": f"{sell_price:.10g}",
                        "realized_profit_USDT": net,
                        "open_inventory_value_USDT": sum(lot_qty[j] * current for j in range(GRID_COUNT) if bought[j]),
                        "cumulative_realized_USDT": realized_net - total_buy_fees,
                    })
        previous = current

    final_price = rows[-1][1]
    open_value = sum(lot_qty[k] * final_price for k in range(GRID_COUNT) if bought[k])
    open_cost = sum(lot_qty[k] * float(buy_prices[k]) for k in range(GRID_COUNT) if bought[k])
    # Net result counts realized cycle gains plus mark-to-market P/L on open lots,
    # with buy and estimated sell fees on remaining inventory.
    open_unrealized_net = sum(
        lot_qty[k] * (final_price - float(buy_prices[k])) - FEE * lot_qty[k] * (float(buy_prices[k]) + final_price)
        for k in range(GRID_COUNT) if bought[k]
    )
    net_pnl = realized_net + open_unrealized_net - total_buy_fees
    summary = {
        "symbol": SYMBOL, "start_snapshot": rows[0][0], "start_currentPrice": start_price,
        "end_snapshot": rows[-1][0], "end_currentPrice": final_price,
        "snapshots_used": len(rows), "priceLow": LOW, "priceHigh": HIGH,
        "grid_count": GRID_COUNT, "grid_mode": "GEOMETRIC", "reference_budget_USDT": BUDGET,
        "fee_per_side_pct": FEE * 100, "completed_cycles": cycles,
        "realized_grid_profit_before_fees_USDT": realized_net,
        "open_inventory_cost_USDT": open_cost, "open_inventory_value_USDT": open_value,
        "open_inventory_unrealized_net_USDT": open_unrealized_net,
        "estimated_net_PnL_USDT": net_pnl, "estimated_net_return_pct": net_pnl / BUDGET * 100,
        "warning": "Approximation from snapshot currentPrice only; not exact Binance execution. No OHLC or actual order fills."
    }
    OUT.mkdir(exist_ok=True)
    pd.DataFrame(events).to_csv(OUT / "tstusdt_grid_replay.csv", index=False)
    pd.DataFrame([summary]).to_csv(OUT / "tstusdt_grid_replay_summary.csv", index=False)
    print(pd.DataFrame([summary]).to_string(index=False))


if __name__ == "__main__":
    main()
