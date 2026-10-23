#!/usr/bin/env python3
"""Simple Claude-style Spot Grid setup from the pair's stored price history."""
from datetime import datetime, timezone
from pathlib import Path
import math
import re

import numpy as np
import pandas as pd

RESULTS = Path("results")
OUTPUT = RESULTS / "current_price.csv"
HISTORY = RESULTS / "history"
HISTORY_DAYS = None   # None = tout l'historique de prix disponible (depuis le 07/10/2026) ; un entier = fenêtre glissante en jours
LOOKBACK_HOURS = 72
MIN_POINTS = 12
GRID_COUNT = 5
MARGIN = 0.03        # marge de 3 % sous le plus bas / au-dessus du plus haut (walk-forward 111 paires : hors-fourchette 8,2 % -> 2,6 %)
GRID_MODE = "GEOMETRIC"
SNAPSHOT_PATTERN = re.compile(r"^(\d{8}T\d{6})Z\.csv\.gz$")

BASE_COLUMNS = [
    "symbol", "minInvestment", "currentPrice", "priceLow", "priceHigh",
    "binancePriceLow", "binancePriceHigh", "Niveau",
]
SETUP_COLUMNS = [
    "recommendedPriceLow", "recommendedPriceHigh", "recommendedGridCount",
    "recommendedGridMode", "rangePosition72hPct", "zScore72h", "setupSignal",
]


def snapshot_timestamp(path):
    match = SNAPSHOT_PATTERN.match(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except ValueError:
        return None


def load_price_history(now):
    rows = []
    for path in HISTORY.rglob("*.csv.gz"):
        timestamp = snapshot_timestamp(path)
        if timestamp is None or timestamp > now + 3600:
            continue
        try:
            frame = pd.read_csv(
                path, compression="gzip",
                usecols=lambda column: column in {"symbol", "currentPrice"},
            )
            if not {"symbol", "currentPrice"}.issubset(frame.columns):
                continue
            frame["symbol"] = frame["symbol"].astype(str).str.strip()
            frame["currentPrice"] = pd.to_numeric(frame["currentPrice"], errors="coerce")
            frame = frame[
                frame["symbol"].ne("")
                & frame["currentPrice"].gt(0)
                & np.isfinite(frame["currentPrice"])
            ]
            for symbol, group in frame.groupby("symbol"):
                rows.append((symbol, timestamp, float(group["currentPrice"].median())))
        except (OSError, ValueError, pd.errors.ParserError):
            continue

    if not rows:
        return {}

    data = pd.DataFrame(rows, columns=["symbol", "timestamp", "price"])
    data = data.drop_duplicates(["symbol", "timestamp"]).sort_values("timestamp")
    return {
        symbol: (group["timestamp"].to_numpy(float), group["price"].to_numpy(float))
        for symbol, group in data.groupby("symbol")
    }


def recommend_setup(timestamps, prices, current_price):
    """Use only observed prices: full-history range plus the 72-hour entry filter."""
    if len(prices) < MIN_POINTS:
        return [None, None, GRID_COUNT, GRID_MODE, None, None, "INSUFFICIENT_HISTORY"]

    recent_mask = (
        np.ones(len(timestamps), dtype=bool) if HISTORY_DAYS is None
        else timestamps >= timestamps[-1] - HISTORY_DAYS * 86400
    )
    recent_prices = prices[recent_mask]
    if len(recent_prices) < MIN_POINTS:
        return [None, None, GRID_COUNT, GRID_MODE, None, None, "INSUFFICIENT_HISTORY"]

    price_low = float(np.min(recent_prices))
    price_high = float(np.max(recent_prices))
    if not (0 < price_low < price_high):
        return [None, None, GRID_COUNT, GRID_MODE, None, None, "INSUFFICIENT_RANGE"]

    recent_72h = prices[timestamps >= timestamps[-1] - LOOKBACK_HOURS * 3600]
    if len(recent_72h) < MIN_POINTS:
        return [price_low, price_high, GRID_COUNT, GRID_MODE, None, None, "INSUFFICIENT_72H"]

    low_72h = float(np.min(recent_72h))
    high_72h = float(np.max(recent_72h))
    position = (
        100.0 * (current_price - low_72h) / (high_72h - low_72h)
        if high_72h > low_72h else None
    )
    deviation = float(np.std(recent_72h))
    z_score = (
        (current_price - float(np.mean(recent_72h))) / deviation
        if deviation > 0 else None
    )

    if (position is not None and position > 80) or (z_score is not None and z_score > 0.3):
        signal = "WAIT_HIGH_RANGE"
    elif current_price < price_low or current_price > price_high:
        signal = "WAIT_OUTSIDE_RANGE"
    else:
        signal = "READY_TO_REVIEW"

    return [
        price_low * (1 - MARGIN), price_high * (1 + MARGIN), GRID_COUNT, GRID_MODE,
        position, z_score, signal,
    ]


def main():
    if not OUTPUT.exists():
        raise SystemExit(f"Missing {OUTPUT}")

    data = pd.read_csv(OUTPUT, dtype={"symbol": str})
    if not {"symbol", "currentPrice"}.issubset(data.columns):
        raise SystemExit("Required columns missing: symbol/currentPrice")

    missing_base = [column for column in BASE_COLUMNS if column not in data.columns]
    if missing_base:
        raise SystemExit(f"Required output columns missing: {missing_base}")

    # Keep only the fields requested for the pair-price file and Claude-style setup.
    data = data[BASE_COLUMNS].copy()
    for column in SETUP_COLUMNS:
        data[column] = None

    history = load_price_history(datetime.now(timezone.utc).timestamp())
    for index, row in data.iterrows():
        symbol = str(row["symbol"]).strip()
        try:
            current_price = float(row["currentPrice"])
        except (TypeError, ValueError):
            current_price = float("nan")

        if symbol not in history or not math.isfinite(current_price) or current_price <= 0:
            data.at[index, "setupSignal"] = "NO_DATA"
            continue

        values = recommend_setup(*history[symbol], current_price)
        for column, value in zip(SETUP_COLUMNS, values):
            data.at[index, column] = value

    temporary = OUTPUT.with_suffix(".csv.tmp")
    data.to_csv(temporary, index=False)
    temporary.replace(OUTPUT)
    print(f"Claude-style historical setup written for {len(data)} pairs")


if __name__ == "__main__":
    main()
