#!/usr/bin/env python3
"""Exploratory, fee-aware Spot Grid range comparison on stored project snapshots.

Simplified grid-order simulator, not an exact Binance engine replay. Each range
uses only prices available before the test; the portfolio is marked to market
after a 48-hour test period.
"""
from datetime import datetime, timezone
from pathlib import Path
import math
import re
import numpy as np
import pandas as pd
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_grid_setup import learn_lower_multiplier

HISTORY = Path("results/history")
CURRENT = Path("results/current_price.csv")
OUT = Path("research_output")
FEE_RATE = 0.001
HOLD_HOURS = 48
WARMUP_DAYS = 7
STEP_HOURS = 24
GRID_COUNTS = (3, 4, 5, 6, 8, 10)
GRID_MODES = ("GEOMETRIC", "ARITHMETIC")
INITIAL_CAPITAL_USDT = 100.0
FIXED_WIDTHS = (0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30, 0.40)
VOL_MULTIPLIERS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
ASYM_MULTIPLIERS = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0)
PATTERN = re.compile(r"^(\d{8}T\d{6})Z\.csv\.gz$")


def file_timestamp(path):
    m = PATTERN.match(path.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except ValueError:
        return None


def load_histories(symbols):
    rows = []
    files = list(HISTORY.rglob("*.csv.gz"))
    for path in files:
        ts = file_timestamp(path)
        if ts is None:
            continue
        try:
            frame = pd.read_csv(
                path, compression="gzip",
                usecols=lambda c: c in {"symbol", "currentPrice"},
            )
            if not {"symbol", "currentPrice"}.issubset(frame.columns):
                continue
            frame["symbol"] = frame["symbol"].astype(str).str.strip()
            frame["currentPrice"] = pd.to_numeric(frame["currentPrice"], errors="coerce")
            frame = frame[
                frame["symbol"].isin(symbols)
                & frame["currentPrice"].gt(0)
                & np.isfinite(frame["currentPrice"])
            ]
            rows.extend((r.symbol, ts, float(r.currentPrice))
                        for r in frame.itertuples(index=False))
        except (OSError, ValueError, pd.errors.ParserError):
            continue
    if not rows:
        return {}, len(files)
    df = pd.DataFrame(rows, columns=["symbol", "timestamp", "price"])
    df = df.groupby(["symbol", "timestamp"], as_index=False)["price"].median()
    return {
        s: (g["timestamp"].to_numpy(float), g["price"].to_numpy(float))
        for s, g in df.groupby("symbol")
    }, len(files)


def sigma_horizon(prices, timestamps, i, hours=96):
    mask = timestamps[:i + 1] >= timestamps[i] - hours * 3600
    p, t = prices[:i + 1][mask], timestamps[:i + 1][mask]
    if len(p) < 12:
        return None
    intervals = np.diff(t)
    intervals = intervals[intervals > 0]
    if not len(intervals):
        return None
    returns = np.diff(np.log(p))
    if len(returns) < 10:
        return None
    step = float(np.median(intervals))
    if step <= 0:
        return None
    return float(np.std(returns, ddof=0) * math.sqrt(hours * 3600 / step))


def simulate(prices, low, high, grid_count, grid_mode):
    start, end = float(prices[0]), float(prices[-1])
    if not (0 < low < start < high) or len(prices) < 2:
        return None
    levels = (np.geomspace(low, high, grid_count + 1)
              if grid_mode == "GEOMETRIC"
              else np.linspace(low, high, grid_count + 1))
    states = [None] * len(levels)
    for i, level in enumerate(levels):
        if level < start:
            states[i] = "buy"
        elif level > start:
            states[i] = "sell"
    buy_levels = [float(levels[i]) for i, side in enumerate(states) if side == "buy"]
    sell_count = sum(side == "sell" for side in states)
    if not buy_levels or sell_count == 0:
        return None
    initial = INITIAL_CAPITAL_USDT
    quote, base = initial / 2, (initial / 2) / start
    qty = min(quote / sum(buy_levels), base / sell_count) * 0.98
    fills, fees, previous = 0, 0.0, start
    for raw in prices[1:]:
        current = float(raw)
        if current < previous:
            crossed = [j for j in range(len(levels))
                       if current <= levels[j] < previous]
            for j in sorted(crossed, reverse=True):
                if states[j] != "buy":
                    continue
                cost = qty * float(levels[j])
                fee = cost * FEE_RATE
                if quote + 1e-12 < cost + fee:
                    continue
                quote -= cost + fee
                base += qty
                fees += fee
                fills += 1
                states[j] = None
                states[j + 1] = "sell"
        elif current > previous:
            crossed = [j for j in range(len(levels))
                       if previous < levels[j] <= current]
            for j in sorted(crossed):
                if states[j] != "sell" or base + 1e-12 < qty:
                    continue
                proceeds = qty * float(levels[j])
                fee = proceeds * FEE_RATE
                quote += proceeds - fee
                base -= qty
                fees += fee
                fills += 1
                states[j] = None
                states[j - 1] = "buy"
        previous = current
    final_value = quote + base * end
    hold_value = initial / 2 + (initial / 2) * end / start
    return {
        "netReturnPct": (final_value / initial - 1) * 100,
        "holdReturnPct": (hold_value / initial - 1) * 100,
        "excessVsHoldPct": (final_value - hold_value) / initial * 100,
        "fills": fills,
        "fees": fees,
    }


def candidates(prices, timestamps, i):
    start, past, ts = float(prices[i]), prices[:i + 1], timestamps[:i + 1]
    result = []
    for width in FIXED_WIDTHS:
        for count in GRID_COUNTS:
            result.append((f"FIXED_{width:.0%}", width, count,
                           start * (1 - width), start * (1 + width)))
    sigma = sigma_horizon(prices, timestamps, i)
    if sigma is not None and math.isfinite(sigma) and sigma > 0:
        for mult in VOL_MULTIPLIERS:
            width = min(mult * sigma, 1.5)
            for count in GRID_COUNTS:
                result.append((f"VOL_{mult:g}x96H", mult, count,
                               start * math.exp(-width), start * math.exp(width)))
    if sigma is not None and math.isfinite(sigma) and sigma > 0:
        for low_mult in ASYM_MULTIPLIERS:
            for high_mult in ASYM_MULTIPLIERS:
                low = start * math.exp(-min(low_mult * sigma, 1.5))
                high = start * math.exp(min(high_mult * sigma, 1.5))
                for count in GRID_COUNTS:
                    result.append((f"ASYM_L{low_mult:g}_H{high_mult:g}",
                                   f"{low_mult:g}/{high_mult:g}", count, low, high))
        # Pre-specified directional rule: widen the side in the direction of the
        # trailing 96h trend, keeping the opposite side tighter.
        mask = timestamps[:i + 1] >= timestamps[i] - 96 * 3600
        recent = prices[:i + 1][mask]
        if len(recent) >= 2:
            trend = float(start / recent[0] - 1)
            if trend > 0.03:
                low_mult, high_mult, regime = 0.75, 2.5, "UP"
            elif trend < -0.03:
                low_mult, high_mult, regime = 2.5, 0.75, "DOWN"
            else:
                low_mult, high_mult, regime = 1.25, 1.25, "FLAT"
            low = start * math.exp(-min(low_mult * sigma, 1.5))
            high = start * math.exp(min(high_mult * sigma, 1.5))
            result.append((f"TREND_ASYM_{regime}", f"{low_mult:g}/{high_mult:g}",
                           5, low, high))

    # Production range logic, recalculated using past data only.
    observed_low, observed_high = float(np.min(past)), float(np.max(past))
    learning = learn_lower_multiplier(ts, past)
    if (learning["active"] and learning["multiplier"] is not None
            and learning["volatility"] is not None):
        low = observed_low * max(0.01, 1 - learning["multiplier"] * learning["volatility"])
    else:
        low = observed_low * 0.97
    high = observed_high * 1.03
    if 0 < low < start < high:
        result.append(("CURRENT_METHOD", None, 5, low, high))
    return [(*candidate, mode) for candidate in result for mode in GRID_MODES]


def main():
    if not CURRENT.exists():
        raise SystemExit(f"Missing {CURRENT}; run the regular audit first.")
    current = pd.read_csv(CURRENT, dtype={"symbol": str})
    # 100 USDT is a reference capital, not a quote-currency filter.
    # Apply the same notional capital in each pair's quote currency without
    # converting between currencies or excluding non-USDT markets.
    current["symbol"] = current["symbol"].astype(str).str.strip()
    symbols = set(current["symbol"])
    if not symbols:
        raise SystemExit("No symbols available in current_price.csv.")
    histories, file_count = load_histories(symbols)
    detail = []
    for symbol, (timestamps, prices) in histories.items():
        if len(prices) < 12:
            continue
        target = timestamps[0] + WARMUP_DAYS * 86400
        last = timestamps[-1] - HOLD_HOURS * 3600
        while target <= last:
            i = int(np.searchsorted(timestamps, target, side="left"))
            if i >= len(timestamps) or timestamps[i] > target + 3 * 3600:
                target += STEP_HOURS * 3600
                continue
            end_i = int(np.searchsorted(
                timestamps, timestamps[i] + HOLD_HOURS * 3600, side="right"
            )) - 1
            if end_i <= i + 2:
                target += STEP_HOURS * 3600
                continue
            test_prices = prices[i:end_i + 1]
            for name, param, count, low, high, mode in candidates(prices, timestamps, i):
                metrics = simulate(test_prices, low, high, count, mode)
                if metrics is None:
                    continue
                detail.append({
                    "symbol": symbol,
                    "startUTC": datetime.fromtimestamp(timestamps[i], timezone.utc).isoformat(),
                    "strategy": name,
                    "parameter": param,
                    "gridCount": count,
                    "gridMode": mode,
                    "lowVsStartPct": (low / prices[i] - 1) * 100,
                    "highVsStartPct": (high / prices[i] - 1) * 100,
                    **metrics,
                })
            target += STEP_HOURS * 3600
    if not detail:
        raise SystemExit("No test windows could be simulated; inspect history coverage.")
    df = pd.DataFrame(detail)
    OUT.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT / "grid_range_research_detail.csv", index=False)
    summary = df.groupby(["strategy", "parameter", "gridCount", "gridMode"], dropna=False).agg(
        testWindows=("excessVsHoldPct", "size"),
        meanNetReturnPct=("netReturnPct", "mean"),
        medianNetReturnPct=("netReturnPct", "median"),
        meanHoldReturnPct=("holdReturnPct", "mean"),
        meanExcessVsHoldPct=("excessVsHoldPct", "mean"),
        medianExcessVsHoldPct=("excessVsHoldPct", "median"),
        positiveNetPct=("netReturnPct", lambda x: float((x > 0).mean() * 100)),
        positiveExcessPct=("excessVsHoldPct", lambda x: float((x > 0).mean() * 100)),
        medianFills=("fills", "median"),
        medianFees=("fees", "median"),
        medianLowVsStartPct=("lowVsStartPct", "median"),
        medianHighVsStartPct=("highVsStartPct", "median"),
    ).reset_index().sort_values(
        ["meanNetReturnPct", "meanExcessVsHoldPct"], ascending=False
    )
    summary.to_csv(OUT / "grid_range_research_summary.csv", index=False)

    # Chronological holdout: choose the best configuration on the first 70% of
    # observed timestamps, then evaluate it only on the later 30%.
    df["_timestamp"] = pd.to_datetime(df["startUTC"], utc=True)
    cutoffs = np.sort(df["_timestamp"].dropna().unique())
    if len(cutoffs) >= 4:
        split_index = min(len(cutoffs) - 1, max(1, int(len(cutoffs) * 0.70)))
        cutoff = pd.Timestamp(cutoffs[split_index])
        train = df[df["_timestamp"] < cutoff].copy()
        holdout = df[df["_timestamp"] >= cutoff].copy()
        keys = ["strategy", "parameter", "gridCount", "gridMode"]
        train_rank = train.groupby(keys, dropna=False).agg(
            trainWindows=("netReturnPct", "size"),
            trainMeanNetReturnPct=("netReturnPct", "mean"),
            trainMeanExcessVsHoldPct=("excessVsHoldPct", "mean"),
        ).reset_index().sort_values(
            ["trainMeanNetReturnPct", "trainMeanExcessVsHoldPct"], ascending=False
        )
        chosen = train_rank.iloc[0]
        chosen_mask = pd.Series(True, index=holdout.index)
        for key in keys:
            if pd.isna(chosen[key]):
                chosen_mask &= holdout[key].isna()
            else:
                chosen_mask &= holdout[key].eq(chosen[key])
        selected_test = holdout[chosen_mask]
        current_test = holdout[holdout["strategy"].eq("CURRENT_METHOD")]
        holdout_rows = []
        for label, part in [("TRAIN_SELECTED_CONFIG_ON_HOLDOUT", selected_test),
                            ("CURRENT_METHOD_ON_HOLDOUT", current_test)]:
            if part.empty:
                continue
            holdout_rows.append({
                "evaluation": label,
                "strategy": chosen["strategy"] if label.startswith("TRAIN_") else "CURRENT_METHOD",
                "parameter": chosen["parameter"] if label.startswith("TRAIN_") else None,
                "gridCount": int(chosen["gridCount"]) if label.startswith("TRAIN_") else 5,
                "gridMode": chosen["gridMode"] if label.startswith("TRAIN_") else None,
                "trainCutoffUTC": cutoff.isoformat(),
                "trainWindows": int(len(train)),
                "holdoutWindows": int(len(part)),
                "meanNetReturnPct": float(part["netReturnPct"].mean()),
                "medianNetReturnPct": float(part["netReturnPct"].median()),
                "meanExcessVsHoldPct": float(part["excessVsHoldPct"].mean()),
                "medianExcessVsHoldPct": float(part["excessVsHoldPct"].median()),
                "positiveNetPct": float((part["netReturnPct"] > 0).mean() * 100),
                "positiveExcessPct": float((part["excessVsHoldPct"] > 0).mean() * 100),
                "medianFills": float(part["fills"].median()),
            })
        holdout_summary = pd.DataFrame(holdout_rows)
        holdout_summary.to_csv(OUT / "grid_range_research_holdout.csv", index=False)
    else:
        cutoff = None
        holdout_summary = pd.DataFrame()
        holdout_summary.to_csv(OUT / "grid_range_research_holdout.csv", index=False)
    best = summary.iloc[0]
    production = summary[summary["strategy"] == "CURRENT_METHOD"]
    lines = [
        "# Spot Grid range research — exploratory",
        "",
        f"- Historical snapshot files read: {file_count}",
        f"- Symbols considered across all quote currencies: {len(symbols)}",
        f"- Pairs with simulations: {df['symbol'].nunique()}",
        f"- Pair × time-window × strategy simulations: {len(df):,}",
        f"- Holding period: {HOLD_HOURS} hours; starting capital: {INITIAL_CAPITAL_USDT:.0f} USDT, split 50% quote / 50% base.",
        f"- Grid counts tested: {list(GRID_COUNTS)}; modes: {list(GRID_MODES)}.",
        f"- Assumed fee: {FEE_RATE * 100:.2f}% per fill.",
        "- Primary objective: highest average net portfolio return; excess vs passive 50/50 hold is also reported.",
        "- Simplified simulator on periodic snapshots, not an exact Binance execution replay.",
        "- A chronological 70/30 holdout is reported separately; the selected configuration is ranked on the earlier training period only.",
        "- Capital is normalized to 100 units for percentage-return comparison; because minimum notional, tick/step sizes and quote-to-USDT conversion are not simulated, this is not proof that every pair is executable with 100 USDT-equivalent.",
        "",
        "## Highest average net return in this sample",
        "",
        f"- Strategy: {best['strategy']}; grid count {int(best['gridCount'])}; mode {best['gridMode']}",
        f"- Mean net return: {best['meanNetReturnPct']:.4f}%",
        f"- Mean excess vs hold: {best['meanExcessVsHoldPct']:.4f} percentage points",
        f"- Median excess vs hold: {best['medianExcessVsHoldPct']:.4f} percentage points",
        f"- Positive net-return windows: {best['positiveNetPct']:.1f}%",
        f"- Median fills per 48h: {best['medianFills']:.1f}",
        "",
        "## Current production method benchmark",
        "",
    ]
    if not production.empty:
        row = production.iloc[0]
        lines += [
            f"- Median excess vs hold: {row['medianExcessVsHoldPct']:.4f} percentage points",
            f"- Mean excess vs hold: {row['meanExcessVsHoldPct']:.4f} percentage points",
            f"- Positive windows: {row['positiveExcessPct']:.1f}%",
            f"- Median fills per 48h: {row['medianFills']:.1f}",
            f"- Median range relative to entry: {row['medianLowVsStartPct']:.2f}% / +{row['medianHighVsStartPct']:.2f}%",
        ]
    else:
        lines.append("- No eligible current-method test windows.")
    lines += [
        "",
        "## Limitations",
        "",
        "1. Periodic snapshots miss intraperiod price paths and some possible fills.",
        "2. Order and inventory handling is simplified versus Binance.",
        "3. Available history is short, so aggregate results have substantial uncertainty.",
        "4. Do not change production recommendations from this comparison alone; validate the chosen family on a later untouched period.",
        "",
        "## Chronological holdout (configuration selected on training period only)",
        "",
        *(
            ["- Holdout unavailable: insufficient distinct timestamps."]
            if holdout_summary.empty else [
                f"- {r['evaluation']}: {int(r['holdoutWindows'])} windows; mean net {r['meanNetReturnPct']:.4f}%; "
                f"mean excess vs hold {r['meanExcessVsHoldPct']:.4f} pp; positive net windows {r['positiveNetPct']:.1f}%."
                for r in holdout_summary.to_dict("records")
            ]
        ),
        "",
        "Full detail, aggregate CSV and chronological holdout CSV are attached to the workflow run.",
    ]
    (OUT / "grid_range_research_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
