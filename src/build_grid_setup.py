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
HISTORY_DAYS = None   # None = tout l'historique de prix disponible ; un entier = fenêtre glissante en jours
LOOKBACK_HOURS = 72
VOLATILITY_HOURS = 96
FORWARD_HOURS = 96
MIN_HISTORY_DAYS = 14
MIN_POINTS = 12
GRID_COUNT = 5
MARGIN = 0.03
TARGET_LOWER_EXIT_RATE = 0.05
LOWER_MULTIPLIERS = (0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0)
GRID_MODE = "GEOMETRIC"
LEARNING_OUTPUT = RESULTS / "current_price_learning.csv"
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


def volatility_ratio(prices):
    """Relative standard deviation of the last 96 hourly observations."""
    values = np.asarray(prices, dtype=float)
    values = values[np.isfinite(values) & (values > 0)]
    if len(values) < MIN_POINTS:
        return None
    mean = float(np.mean(values))
    if mean <= 0:
        return None
    return float(np.std(values, ddof=0) / mean)


def learn_lower_multiplier(timestamps, prices):
    """Select the smallest volatility multiplier whose historical lower-break rate is <= 5%.

    Each training episode uses only prices known at its start to set its range,
    and is scored against the following 96 hours. Episodes are spaced 6 hours apart.
    """
    timestamps = np.asarray(timestamps, dtype=float)
    prices = np.asarray(prices, dtype=float)
    valid = np.isfinite(timestamps) & np.isfinite(prices) & (prices > 0)
    timestamps, prices = timestamps[valid], prices[valid]
    order = np.argsort(timestamps)
    timestamps, prices = timestamps[order], prices[order]
    if len(prices) < MIN_POINTS:
        return {"active": False, "multiplier": None, "volatility": None,
                "episodes": 0, "lower_exit_rate": None, "target_met": False,
                "reason": "INSUFFICIENT_HISTORY", "candidate_rates": {}}

    duration_days = (timestamps[-1] - timestamps[0]) / 86400
    current_volatility = volatility_ratio(prices[timestamps >= timestamps[-1] - VOLATILITY_HOURS * 3600])
    if duration_days < MIN_HISTORY_DAYS:
        return {"active": False, "multiplier": None, "volatility": current_volatility,
                "episodes": 0, "lower_exit_rate": None, "target_met": False,
                "reason": "LESS_THAN_14_DAYS", "candidate_rates": {}}

    intervals = np.diff(timestamps)
    typical_step = float(np.median(intervals)) if len(intervals) else 0
    if typical_step <= 0:
        return {"active": False, "multiplier": None, "volatility": current_volatility,
                "episodes": 0, "lower_exit_rate": None, "target_met": False,
                "reason": "INVALID_TIMESTAMPS", "candidate_rates": {}}
    step = max(1, int(round(21600 / typical_step)))
    horizon = max(MIN_POINTS, int(round(FORWARD_HOURS * 3600 / typical_step)))
    vol_window = max(MIN_POINTS, int(round(VOLATILITY_HOURS * 3600 / typical_step)))
    episodes = []
    for i in range(vol_window, len(prices) - horizon + 1, step):
        past = prices[:i]
        recent = prices[max(0, i - vol_window):i]
        vol = volatility_ratio(recent)
        if vol is None:
            continue
        known_low = float(np.min(past))
        future_low = float(np.min(prices[i:i + horizon]))
        episodes.append((known_low, vol, future_low))

    if len(episodes) < 5:
        return {"active": False, "multiplier": None, "volatility": current_volatility,
                "episodes": len(episodes), "lower_exit_rate": None, "target_met": False,
                "reason": "INSUFFICIENT_COMPLETED_EPISODES", "candidate_rates": {}}

    candidate_rates = {}
    for multiplier in LOWER_MULTIPLIERS:
        exits = sum(
            future_low < known_low * max(0.01, 1 - multiplier * vol)
            for known_low, vol, future_low in episodes
        )
        candidate_rates[str(multiplier)] = exits / len(episodes)

    eligible = [m for m in LOWER_MULTIPLIERS
                if candidate_rates[str(m)] <= TARGET_LOWER_EXIT_RATE]
    selected = min(eligible) if eligible else max(LOWER_MULTIPLIERS)
    target_met = bool(eligible)
    return {"active": current_volatility is not None, "multiplier": selected,
            "volatility": current_volatility, "episodes": len(episodes),
            "lower_exit_rate": candidate_rates[str(selected)], "target_met": target_met,
            "reason": "OK" if target_met else "TARGET_NOT_MET_MAX_MULTIPLIER_USED",
            "candidate_rates": candidate_rates}


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

    observed_low = float(np.min(recent_prices))
    price_high = float(np.max(recent_prices))
    if not (0 < observed_low < price_high):
        return [None, None, GRID_COUNT, GRID_MODE, None, None, "INSUFFICIENT_RANGE"]

    learning = learn_lower_multiplier(timestamps, prices)
    if learning["active"] and learning["multiplier"] is not None and learning["volatility"] is not None:
        price_low = observed_low * max(0.01, 1 - learning["multiplier"] * learning["volatility"])
    else:
        price_low = observed_low * (1 - MARGIN)

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
        price_low, price_high * (1 + MARGIN), GRID_COUNT, GRID_MODE,
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
    learning_rows = []
    for index, row in data.iterrows():
        symbol = str(row["symbol"]).strip()
        try:
            current_price = float(row["currentPrice"])
        except (TypeError, ValueError):
            current_price = float("nan")

        if symbol not in history or not math.isfinite(current_price) or current_price <= 0:
            data.at[index, "setupSignal"] = "NO_DATA"
            continue

        timestamps, prices = history[symbol]
        learning = learn_lower_multiplier(timestamps, prices)
        values = recommend_setup(timestamps, prices, current_price)
        for column, value in zip(SETUP_COLUMNS, values):
            data.at[index, column] = value
        learning_rows.append({
            "symbol": symbol,
            "historyDays": round((timestamps[-1] - timestamps[0]) / 86400, 3) if len(timestamps) else 0,
            "learningActive": learning["active"],
            "learningReason": learning["reason"],
            "episodes": learning["episodes"],
            "volatility96hPct": learning["volatility"] * 100 if learning["volatility"] is not None else None,
            "selectedMultiplier": learning["multiplier"],
            "lowerExitRatePct": learning["lower_exit_rate"] * 100 if learning["lower_exit_rate"] is not None else None,
            "targetLowerExitRatePct": TARGET_LOWER_EXIT_RATE * 100,
            "targetMet": learning["target_met"],
            "candidateExitRatesPct": ";".join(
                f"{key}:{value * 100:.2f}" for key, value in learning["candidate_rates"].items()
            ),
        })

    temporary = OUTPUT.with_suffix(".csv.tmp")
    data.to_csv(temporary, index=False)
    temporary.replace(OUTPUT)
    pd.DataFrame(learning_rows, columns=[
        "symbol", "historyDays", "learningActive", "learningReason", "episodes",
        "volatility96hPct", "selectedMultiplier", "lowerExitRatePct",
        "targetLowerExitRatePct", "targetMet", "candidateExitRatesPct"
    ]).sort_values("symbol").to_csv(LEARNING_OUTPUT, index=False)
    print(f"Historical adaptive setup written for {len(data)} pairs; learning diagnostics: {LEARNING_OUTPUT}")


if __name__ == "__main__":
    main()
