#!/usr/bin/env python3
"""Build Spot Grid setup suggestions from the repository's own hourly price snapshots.

This is a research aid, not an order generator. It does not call an exchange API and
never changes the source history or current_multicriteria.csv.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import csv
import math
import re

import numpy as np
import pandas as pd

RESULTS = Path("results")
CURRENT = RESULTS / "current_price.csv"
HISTORY = RESULTS / "history"
VALIDATION = RESULTS / "grid_setup_validation.csv"
FEE = 0.001                 # per order, conservative public-marketplace reference
BUDGET = 100.0              # quote currency used to compare candidate rules
LOOKBACK_HOURS = 72
FORWARD_HOURS = 96
WINDOW_STEP_HOURS = 24
HISTORY_DAYS = 30
BUFFERS = (0.02, 0.04, 0.06)
GRID_COUNTS = (5, 6, 8, 10)
TIME_RE = re.compile(r"^(\d{8}T\d{6})Z\.csv\.gz$")

NEW_COLUMNS = [
    "recommendedPriceLow", "recommendedPriceHigh", "recommendedGridCount",
    "recommendedGridMode", "setupRuleBufferPct", "rangePosition72hPct",
    "zScore72h", "rangeWidth72hPct", "setupSignal", "setupReason",
    "setupHistoryPoints", "backtest4dMeanProfitPct", "backtest4dWinRatePct",
    "backtest4dOutsidePct", "backtest4dWindows",
]


def snapshot_time(path):
    match = TIME_RE.match(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_history(now):
    cutoff = now - timedelta(days=HISTORY_DAYS)
    rows = []
    files = sorted(HISTORY.rglob("*.csv.gz"))
    for path in files:
        stamp = snapshot_time(path)
        if stamp is None or stamp < cutoff or stamp > now + timedelta(hours=1):
            continue
        try:
            frame = pd.read_csv(
                path, compression="gzip",
                usecols=lambda c: c in {"symbol", "currentPrice"},
                dtype={"symbol": str},
            )
            if not {"symbol", "currentPrice"}.issubset(frame.columns):
                continue
            frame["symbol"] = frame["symbol"].astype(str).str.strip()
            frame["currentPrice"] = pd.to_numeric(frame["currentPrice"], errors="coerce")
            frame = frame[
                frame["symbol"].ne("") & frame["currentPrice"].gt(0)
                & np.isfinite(frame["currentPrice"])
            ]
            if frame.empty:
                continue
            # Multiple bots can reference the same pair in a snapshot; median avoids duplicates.
            grouped = frame.groupby("symbol", as_index=False)["currentPrice"].median()
            for item in grouped.itertuples(index=False):
                rows.append((item.symbol, stamp.timestamp(), float(item.currentPrice)))
        except (OSError, ValueError, pd.errors.ParserError):
            continue
    if not rows:
        return {}
    hist = pd.DataFrame(rows, columns=["symbol", "timestamp", "price"])
    hist = hist.drop_duplicates(["symbol", "timestamp"], keep="last")
    result = {}
    for symbol, group in hist.groupby("symbol"):
        group = group.sort_values("timestamp")
        result[symbol] = (
            group["timestamp"].to_numpy(dtype=float),
            group["price"].to_numpy(dtype=float),
        )
    return result


def make_levels(low, high, count):
    if not (math.isfinite(low) and math.isfinite(high) and 0 < low < high):
        return None
    return np.geomspace(low, high, count + 1)


def simulate_grid(prices, low, high, count):
    """Approximate realized grid-cycle profit; inventory mark-to-market is not included."""
    levels = make_levels(low, high, count)
    if levels is None or len(prices) < 2:
        return None
    bought = [False] * count
    profit = 0.0
    cycles = 0
    qty = [BUDGET / count / levels[i] for i in range(count)]
    for previous, current in zip(prices[:-1], prices[1:]):
        if current < previous:
            # A falling price fills buy levels in descending order.
            crossed = [k for k in range(count) if previous > levels[k] >= current]
            for k in reversed(crossed):
                bought[k] = True
        elif current > previous:
            # A rising price sells the cell immediately below each crossed level.
            crossed = [k for k in range(1, count + 1) if previous < levels[k] <= current]
            for k in crossed:
                cell = k - 1
                if bought[cell]:
                    gross = levels[cell + 1] - levels[cell]
                    fees = FEE * (levels[cell + 1] + levels[cell])
                    profit += qty[cell] * (gross - fees)
                    cycles += 1
                    bought[cell] = False
    outside = sum(1 for price in prices if price < low or price > high) / len(prices) * 100.0
    return {
        "profit_pct": profit / BUDGET * 100.0,
        "cycles": cycles,
        "outside_pct": outside,
    }


def candidate_windows(series):
    times, prices = series
    if len(times) < 20:
        return []
    first, last = times[0], times[-1]
    lookback = LOOKBACK_HOURS * 3600
    forward = FORWARD_HOURS * 3600
    step = WINDOW_STEP_HOURS * 3600
    windows = []
    next_start = first + lookback
    while next_start + forward <= last:
        # Use the closest recorded observation to the intended start time.
        start_idx = int(np.searchsorted(times, next_start, side="left"))
        if start_idx >= len(times):
            break
        start_time = times[start_idx]
        if start_time + forward > last:
            break
        before = (times >= start_time - lookback) & (times <= start_time)
        after = (times >= start_time) & (times <= start_time + forward)
        past = prices[before]
        future = prices[after]
        if len(past) >= 12 and len(future) >= 12:
            windows.append({
                "start": float(start_time),
                "past": past.copy(),
                "future": future.copy(),
                "low72": float(np.min(past)),
                "high72": float(np.max(past)),
            })
        next_start = start_time + step
    return windows


def evaluate_rule(window, buffer, count):
    low = window["low72"] * (1.0 - buffer)
    high = window["high72"] * (1.0 + buffer)
    if not (0 < low < high):
        return None
    result = simulate_grid(window["future"], low, high, count)
    if result is None:
        return None
    result.update({"low": low, "high": high})
    return result


def build_validation(series_by_symbol):
    rows = []
    per_pair_windows = {}
    for symbol, series in series_by_symbol.items():
        windows = candidate_windows(series)
        if not windows:
            continue
        # Chronological split: older windows select the rule; the latest window(s) are held out.
        split = max(1, int(math.ceil(len(windows) * 0.70)))
        per_pair_windows[symbol] = {
            "train": windows[:split],
            "test": windows[split:],
            "all": windows,
        }

    candidates = []
    for buffer in BUFFERS:
        for count in GRID_COUNTS:
            train_results = []
            test_results = []
            for symbol, bundle in per_pair_windows.items():
                for window in bundle["train"]:
                    value = evaluate_rule(window, buffer, count)
                    if value is not None:
                        train_results.append((symbol, value))
                for window in bundle["test"]:
                    value = evaluate_rule(window, buffer, count)
                    if value is not None:
                        test_results.append((symbol, value))
            if not train_results:
                continue
            mean_profit = float(np.mean([v["profit_pct"] for _, v in train_results]))
            win_rate = float(np.mean([v["profit_pct"] > 0 for _, v in train_results]) * 100.0)
            outside = float(np.mean([v["outside_pct"] for _, v in train_results]))
            pair_means = {}
            for symbol, value in train_results:
                pair_means.setdefault(symbol, []).append(value["profit_pct"])
            pair_mean_profit = float(np.mean([np.mean(values) for values in pair_means.values()]))
            test_profit = float(np.mean([v["profit_pct"] for _, v in test_results])) if test_results else float("nan")
            test_win = float(np.mean([v["profit_pct"] > 0 for _, v in test_results]) * 100.0) if test_results else float("nan")
            test_outside = float(np.mean([v["outside_pct"] for _, v in test_results])) if test_results else float("nan")
            candidates.append({
                "bufferPct": buffer * 100.0,
                "gridCount": count,
                "trainPairs": len(pair_means),
                "trainWindows": len(train_results),
                "trainMeanProfitProxyPct": mean_profit,
                "trainMeanPerPairProfitProxyPct": pair_mean_profit,
                "trainWinRatePct": win_rate,
                "trainOutsidePct": outside,
                "holdoutPairs": len({symbol for symbol, _ in test_results}),
                "holdoutWindows": len(test_results),
                "holdoutMeanProfitProxyPct": test_profit,
                "holdoutWinRatePct": test_win,
                "holdoutOutsidePct": test_outside,
            })

    if not candidates:
        return per_pair_windows, None, pd.DataFrame(columns=[
            "bufferPct", "gridCount", "trainPairs", "trainWindows",
            "trainMeanProfitProxyPct", "trainMeanPerPairProfitProxyPct",
            "trainWinRatePct", "trainOutsidePct", "holdoutPairs", "holdoutWindows",
            "holdoutMeanProfitProxyPct", "holdoutWinRatePct", "holdoutOutsidePct",
        ])

    # Select on training only. Prefer rules with >=50% positive windows and <=20% time out of range.
    eligible = [r for r in candidates if r["trainWinRatePct"] >= 50 and r["trainOutsidePct"] <= 20]
    if eligible:
        chosen = max(eligible, key=lambda r: (
            r["trainMeanPerPairProfitProxyPct"], -r["trainOutsidePct"], -r["gridCount"]
        ))
    else:
        chosen = max(candidates, key=lambda r: (
            r["trainWinRatePct"], -r["trainOutsidePct"], r["trainMeanPerPairProfitProxyPct"], -r["gridCount"]
        ))
    summary = pd.DataFrame(sorted(candidates, key=lambda r: (r["bufferPct"], r["gridCount"])))
    return per_pair_windows, chosen, summary


def add_setup_columns(current, series_by_symbol, per_pair_windows, chosen, now):
    result = current.copy()
    for column in NEW_COLUMNS:
        # Pandas 3 infers dtype=str from an empty-string initializer; use object
        # so the same output column can hold numbers, labels, or missing values.
        result[column] = pd.Series([None] * len(result), index=result.index, dtype=object)
    # If the replay cannot select a rule, use a clearly marked fallback, not a claimed optimum.
    buffer = chosen["bufferPct"] / 100.0 if chosen else 0.04
    count = int(chosen["gridCount"]) if chosen else 6

    for idx, row in result.iterrows():
        symbol = str(row.get("symbol", "")).strip()
        current_price = pd.to_numeric(pd.Series([row.get("currentPrice")]), errors="coerce").iloc[0]
        series = series_by_symbol.get(symbol)
        if series is None or not math.isfinite(float(current_price)) or current_price <= 0:
            result.at[idx, "setupSignal"] = "NO_DATA"
            result.at[idx, "setupReason"] = "Pas assez de snapshots historiques exploitables."
            continue

        times, prices = series
        latest_time = times[-1]
        recent = (times >= latest_time - LOOKBACK_HOURS * 3600) & (times <= latest_time)
        recent_prices = prices[recent]
        span_hours = (times[-1] - times[0]) / 3600.0
        if len(recent_prices) < 12 or (times[-1] - times[recent][0]) < 48 * 3600:
            result.at[idx, "setupSignal"] = "INSUFFICIENT_HISTORY"
            result.at[idx, "setupReason"] = "Il faut au moins 48 h de snapshots récents pour calculer les indicateurs 72 h."
            result.at[idx, "setupHistoryPoints"] = int(len(recent_prices))
            continue

        low72 = float(np.min(recent_prices))
        high72 = float(np.max(recent_prices))
        low = low72 * (1.0 - buffer)
        high = high72 * (1.0 + buffer)
        mean = float(np.mean(recent_prices))
        std = float(np.std(recent_prices, ddof=0))
        position = ((float(current_price) - low72) / (high72 - low72) * 100.0) if high72 > low72 else 50.0
        zscore = (float(current_price) - mean) / std if std > 0 else 0.0
        width_pct = (high72 / low72 - 1.0) * 100.0 if low72 > 0 else float("nan")

        bundle = per_pair_windows.get(symbol, {})
        tests = bundle.get("test", [])
        test_values = [evaluate_rule(window, buffer, count) for window in tests]
        test_values = [v for v in test_values if v is not None]
        mean_profit = float(np.mean([v["profit_pct"] for v in test_values])) if test_values else float("nan")
        win_rate = float(np.mean([v["profit_pct"] > 0 for v in test_values]) * 100.0) if test_values else float("nan")
        outside = float(np.mean([v["outside_pct"] for v in test_values])) if test_values else float("nan")

        if not chosen:
            signal = "RULE_NOT_VALIDATED"
            reason = "Historique insuffisant pour départager les réglages; valeurs indicatives uniquement."
        elif position >= 80.0 or zscore > 0.3:
            signal = "WAIT_HIGH_RANGE"
            reason = "Prix trop haut dans son range 72 h ou au-dessus de sa moyenne ajustée du risque; attendre un retour."
        elif float(current_price) < low or float(current_price) > high:
            signal = "WAIT_OUTSIDE_RANGE"
            reason = "Le prix actuel est hors de la fourchette proposée; ne pas lancer la grille maintenant."
        else:
            signal = "READY_TO_REVIEW"
            reason = "Position 72 h acceptable et prix dans la fourchette; vérifier frais, liquidité et risque avant lancement."

        values = {
            "recommendedPriceLow": low,
            "recommendedPriceHigh": high,
            "recommendedGridCount": count,
            "recommendedGridMode": "GEOMETRIC",
            "setupRuleBufferPct": buffer * 100.0,
            "rangePosition72hPct": position,
            "zScore72h": zscore,
            "rangeWidth72hPct": width_pct,
            "setupSignal": signal,
            "setupReason": reason,
            "setupHistoryPoints": int(len(recent_prices)),
            "backtest4dMeanProfitPct": mean_profit,
            "backtest4dWinRatePct": win_rate,
            "backtest4dOutsidePct": outside,
            "backtest4dWindows": len(test_values),
        }
        for column, value in values.items():
            result.at[idx, column] = value

    # Keep all original columns in their original order, then append the setup columns.
    original = [c for c in current.columns if c not in NEW_COLUMNS]
    return result[original + NEW_COLUMNS]


def main():
    if not CURRENT.exists():
        raise SystemExit(f"Fichier absent: {CURRENT}")
    current = pd.read_csv(CURRENT, dtype={"symbol": str})
    required = {"symbol", "currentPrice"}
    if not required.issubset(current.columns):
        raise SystemExit(f"Colonnes obligatoires absentes de {CURRENT}: {sorted(required - set(current.columns))}")

    now = datetime.now(timezone.utc)
    history = load_history(now)
    # Use only symbols represented in the current file, while preserving the complete stored history.
    history = {symbol: series for symbol, series in history.items() if symbol in set(current["symbol"].astype(str))}
    per_pair_windows, chosen, validation = build_validation(history)
    validation.to_csv(VALIDATION, index=False)
    output = add_setup_columns(current, history, per_pair_windows, chosen, now)
    temp = CURRENT.with_suffix(".csv.tmp")
    output.to_csv(temp, index=False)
    temp.replace(CURRENT)

    if chosen:
        print(
            "GRID_SETUP_RULE_SELECTED "
            f"buffer={chosen['bufferPct']:.0f}% grids={chosen['gridCount']} geometric; "
            f"train_pairs={chosen['trainPairs']} train_windows={chosen['trainWindows']} "
            f"train_mean_profit_proxy={chosen['trainMeanPerPairProfitProxyPct']:.4f}% "
            f"train_win_rate={chosen['trainWinRatePct']:.1f}% train_outside={chosen['trainOutsidePct']:.1f}%; "
            f"holdout_pairs={chosen['holdoutPairs']} holdout_windows={chosen['holdoutWindows']} "
            f"holdout_mean_profit_proxy={chosen['holdoutMeanProfitProxyPct']:.4f}% "
            f"holdout_win_rate={chosen['holdoutWinRatePct']:.1f}% holdout_outside={chosen['holdoutOutsidePct']:.1f}%"
        )
    else:
        print("GRID_SETUP_RULE_FALLBACK buffer=4% grids=6; no rule could be validated from stored history.")
    print(f"Grid setup appended to {CURRENT}; rule comparison saved to {VALIDATION}; symbols with history={len(history)}.")


if __name__ == "__main__":
    main()
