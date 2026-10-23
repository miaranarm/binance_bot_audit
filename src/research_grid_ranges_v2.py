#!/usr/bin/env python3
"""Fee-aware, candle-based research for Spot Grid parameter families.

Research only: this program does not place orders and does not change production
recommendations. It compares families on chronological, pair-level blocks.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json, math, time
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "results" / "history"
CURRENT = ROOT / "results" / "current_price.csv"
OUT = ROOT / "research_output_v2"
API = ("https://data-api.binance.vision/api/v3/klines",
       "https://api.binance.com/api/v3/klines",
       "https://api-gcp.binance.com/api/v3/klines")
INTERVAL = "15m"
INTERVAL_MS = 900_000
FEE = 0.001
CAPITAL_USDT = 100.0
MIN_ORDER_USDT = 5.0
MIN_STEP_PCT = 0.006
MAX_GRIDS = 20
GRID_COUNTS = (3, 4, 5, 6, 8, 10, 12, 16, 20)
WIDTHS_SIGMA = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
ASYMMETRIES = ((1.0, 1.0), (0.75, 1.5), (1.5, 0.75), (0.5, 2.0), (2.0, 0.5))
MAX_HISTORY_DAYS = 30
MIN_CANDLES = 96


def snapshot_ts(path):
    try:
        return datetime.strptime(path.name[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def load_snapshots(symbols):
    rows = []
    for p in HISTORY.rglob("*.csv.gz"):
        ts = snapshot_ts(p)
        if ts is None:
            continue
        try:
            d = pd.read_csv(p, compression="gzip", usecols=lambda c: c in {"symbol", "currentPrice"})
            if not {"symbol", "currentPrice"}.issubset(d.columns):
                continue
            d["currentPrice"] = pd.to_numeric(d["currentPrice"], errors="coerce")
            d = d[d.symbol.astype(str).isin(symbols) & d.currentPrice.gt(0)]
            rows.extend((str(r.symbol), ts, float(r.currentPrice)) for r in d.itertuples(index=False))
        except (OSError, ValueError, pd.errors.ParserError):
            continue
    if not rows:
        return {}
    d = pd.DataFrame(rows, columns=["symbol", "ts", "price"]).drop_duplicates(["symbol", "ts"])
    return {s: g.sort_values("ts") for s, g in d.groupby("symbol")}


def get_klines(symbol, start_ms, end_ms):
    all_rows, cursor = [], start_ms
    while cursor < end_ms:
        params = urlencode({"symbol": symbol, "interval": INTERVAL, "startTime": cursor,
                            "endTime": end_ms, "limit": 1000})
        payload = None
        for api in API:
            try:
                req = Request(api + "?" + params, headers={"User-Agent": "spot-grid-range-research/2.0"})
                with urlopen(req, timeout=12) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                if isinstance(payload, list):
                    break
                payload = None
            except Exception:
                payload = None
        if payload is None:
            raise RuntimeError("BINANCE_KLINES_UNAVAILABLE")
        if not payload:
            break
        all_rows.extend(payload)
        nxt = int(payload[-1][0]) + INTERVAL_MS
        if nxt <= cursor:
            raise RuntimeError("KLINE_PAGINATION_STALLED")
        cursor = nxt
        if len(payload) < 1000:
            break
        time.sleep(0.05)
    if not all_rows:
        return np.empty((0, 6))
    a = np.array([[int(x[0]), *map(float, (x[1], x[2], x[3], x[4], x[5]))] for x in all_rows], dtype=float)
    return a[np.argsort(a[:, 0])]


def quote_to_usdt(symbol, quote, prices):
    if quote == "USDT":
        return 1.0
    # Use a current conversion pair only when it is explicitly present in the snapshot.
    for candidate in (quote + "USDT", quote + "USDC", quote + "FDUSD"):
        if candidate in prices and prices[candidate] > 0:
            return prices[candidate] if candidate.endswith("USDT") else None
    return None


def quote_asset(symbol):
    # Longest known quote suffix first; this is parsing, not a market filter.
    suffixes = ("USDT", "USDC", "FDUSD", "TUSD", "USDP", "USD1", "BTC", "ETH", "BNB", "EUR", "TRY", "BRL", "BTC", "ETH")
    for q in sorted(set(suffixes), key=len, reverse=True):
        if symbol.endswith(q) and len(symbol) > len(q):
            return q
    return None


def simulate(close, low, high, n, mode, capital_quote, quote_usdt):
    if len(close) < 2 or not (0 < low < close[0] < high) or n < 2:
        return None
    levels = np.geomspace(low, high, n + 1) if mode == "GEOMETRIC" else np.linspace(low, high, n + 1)
    step_pct = np.diff(levels) / levels[:-1]
    if float(np.min(step_pct)) < MIN_STEP_PCT:
        return None
    # Budget per grid cell must clear the $5 equivalent minimum order.
    per_order_quote = capital_quote / (2 * n)
    if per_order_quote * quote_usdt < MIN_ORDER_USDT:
        return None
    base = capital_quote / 2 / close[0]
    quote = capital_quote / 2
    qty = per_order_quote / close[0]
    # Each grid level has a resting order side; a fill flips that level to the opposite side.
    side = ["buy" if x < close[0] else "sell" if x > close[0] else None for x in levels]
    fills = cycles = 0
    cycle_profit = fees = 0.0
    prev = close[0]
    for px in close[1:]:
        px = float(px)
        if px < prev:
            for j in range(n - 1, -1, -1):
                if px <= levels[j] < prev and side[j] == "buy":
                    cost = qty * levels[j]
                    fee = cost * FEE
                    if quote >= cost + fee:
                        quote -= cost + fee; base += qty; fees += fee; fills += 1
                        side[j] = None; side[j + 1] = "sell"
        elif px > prev:
            for j in range(1, n + 1):
                if prev < levels[j] <= px and side[j] == "sell":
                    proceeds = qty * levels[j]
                    fee = proceeds * FEE
                    if base >= qty:
                        quote += proceeds - fee; base -= qty; fees += fee; fills += 1
                        side[j] = None; side[j - 1] = "buy"
                        cycle_profit += qty * (levels[j] - levels[j-1]) - FEE * qty * (levels[j] + levels[j-1])
                        cycles += 1
        prev = px
    final = quote + base * close[-1]
    passive = capital_quote / 2 + capital_quote / 2 * close[-1] / close[0]
    return {"netReturnPct": 100*(final/capital_quote-1),
            "passiveReturnPct": 100*(passive/capital_quote-1),
            "excessVsPassivePct": 100*(final-passive)/capital_quote,
            "cycleProfitPct": 100*cycle_profit/capital_quote,
            "cycles": cycles, "fills": fills, "feesQuote": fees,
            "minStepPct": 100*float(np.min(step_pct))}


def main():
    current = pd.read_csv(CURRENT, dtype={"symbol": str})
    if not {"symbol", "currentPrice"}.issubset(current.columns):
        raise SystemExit("current_price.csv lacks symbol/currentPrice")
    current["symbol"] = current.symbol.astype(str).str.strip()
    current["currentPrice"] = pd.to_numeric(current.currentPrice, errors="coerce")
    prices = dict(zip(current.symbol, current.currentPrice))
    snapshots = load_snapshots(set(current.symbol))
    detail, errors = [], []
    for symbol, hist in snapshots.items():
        hist = hist.dropna(subset=["ts", "price"]).sort_values("ts")
        if len(hist) < 12:
            continue
        start_ms = int(max(hist.ts.iloc[0], hist.ts.iloc[-1] - MAX_HISTORY_DAYS*86400)*1000)
        end_ms = int(hist.ts.iloc[-1]*1000)
        quote = quote_asset(symbol)
        conversion = quote_to_usdt(symbol, quote, prices) if quote else None
        if conversion is None:
            errors.append({"symbol": symbol, "reason": "QUOTE_TO_USDT_UNAVAILABLE", "quote": quote or ""})
            continue
        capital_quote = CAPITAL_USDT / conversion
        try:
            candles = get_klines(symbol, start_ms, end_ms)
        except Exception as e:
            errors.append({"symbol": symbol, "reason": str(e), "quote": quote})
            continue
        if len(candles) < MIN_CANDLES:
            errors.append({"symbol": symbol, "reason": "INSUFFICIENT_15M_CANDLES", "quote": quote})
            continue
        close, ts = candles[:, 4], candles[:, 0]
        # Chronological blocks, each 48h; parameters use only candles preceding each block.
        first_test = max(96, int(len(close)*0.35))
        for i in range(first_test, len(close)-192, 96):
            train = close[max(0, i-96*4):i]
            test = close[i:min(i+192, len(close))]
            if len(test) < 96 or len(train) < 48:
                continue
            logret = np.diff(np.log(train))
            sigma_48h = float(np.std(logret) * math.sqrt(192)) if len(logret) else 0
            if not math.isfinite(sigma_48h) or sigma_48h <= 0:
                continue
            start = float(test[0])
            candidates = []
            for width_mult in WIDTHS_SIGMA:
                for asym_low, asym_high in ASYMMETRIES:
                    low = start * math.exp(-min(width_mult*sigma_48h*asym_low, 1.0))
                    high = start * math.exp(min(width_mult*sigma_48h*asym_high, 1.0))
                    for n in GRID_COUNTS:
                        if n <= min(MAX_GRIDS, int(CAPITAL_USDT/MIN_ORDER_USDT)):
                            for mode in ("GEOMETRIC", "ARITHMETIC"):
                                result = simulate(test, low, high, n, mode, capital_quote, conversion)
                                if result is not None:
                                    candidates.append((width_mult, asym_low, asym_high, n, mode, low, high, result))
            for w, al, ah, n, mode, low, high, r in candidates:
                detail.append({"symbol": symbol, "startUTC": datetime.fromtimestamp(ts[i]/1000, timezone.utc).isoformat(),
                    "strategyFamily": "VOL_ASYMMETRIC", "widthSigma": w, "lowAsymmetry": al,
                    "highAsymmetry": ah, "gridCount": n, "gridMode": mode, "quoteAsset": quote,
                    "quoteToUSDT": conversion, "capitalQuote": capital_quote,
                    "lowerPct": 100*(low/start-1), "upperPct": 100*(high/start-1), **r})
    if not detail:
        raise SystemExit(f"No eligible candle simulations. Inspect {len(errors)} data-quality exclusions.")
    OUT.mkdir(parents=True, exist_ok=True)
    d = pd.DataFrame(detail)
    d.to_csv(OUT/"grid_range_research_detail.csv", index=False)
    keys = ["strategyFamily","widthSigma","lowAsymmetry","highAsymmetry","gridCount","gridMode"]
    summary = d.groupby(keys, dropna=False).agg(
        testWindows=("symbol","size"), pairs=("symbol","nunique"),
        meanCycleProfitPct=("cycleProfitPct","mean"), medianCycleProfitPct=("cycleProfitPct","median"),
        meanNetReturnPct=("netReturnPct","mean"), medianNetReturnPct=("netReturnPct","median"),
        meanExcessVsPassivePct=("excessVsPassivePct","mean"), medianExcessVsPassivePct=("excessVsPassivePct","median"),
        positiveCycleProfitPct=("cycleProfitPct",lambda x:100*(x>0).mean()),
        medianCycles=("cycles","median"), medianFills=("fills","median"), medianFeesQuote=("feesQuote","median")
    ).reset_index()
    # Require cross-pair breadth and rank primarily by cycle profit; total return is a risk diagnostic.
    summary = summary[summary.pairs >= max(3, int(d.symbol.nunique()*0.10))]
    summary = summary.sort_values(["meanCycleProfitPct","medianCycleProfitPct","meanExcessVsPassivePct"], ascending=False)
    summary.to_csv(OUT/"grid_range_research_summary.csv", index=False)
    d["blockTime"] = pd.to_datetime(d.startUTC, utc=True)
    cutoff = d.blockTime.quantile(.70)
    train = d[d.blockTime < cutoff]
    test = d[d.blockTime >= cutoff]
    hold_rows = []
    if not train.empty and not test.empty and not summary.empty:
        train_sum = train.groupby(keys, dropna=False).agg(trainMeanCycleProfitPct=("cycleProfitPct","mean"),
            trainWindows=("symbol","size"), trainPairs=("symbol","nunique")).reset_index()
        train_sum = train_sum[train_sum.trainPairs >= max(3, int(train.symbol.nunique()*0.10))]
        if not train_sum.empty:
            chosen = train_sum.sort_values("trainMeanCycleProfitPct", ascending=False).iloc[0]
            mask = pd.Series(True, index=test.index)
            for k in keys:
                mask &= test[k].eq(chosen[k])
            hold = test[mask]
            if not hold.empty:
                hold_rows.append({"evaluation":"TRAIN_SELECTED_ON_LATER_TIME_BLOCK","trainCutoffUTC":cutoff.isoformat(),
                    **{k:chosen[k] for k in keys}, "trainWindows":int(chosen.trainWindows),
                    "holdoutWindows":len(hold), "holdoutPairs":hold.symbol.nunique(),
                    "meanCycleProfitPct":hold.cycleProfitPct.mean(),"medianCycleProfitPct":hold.cycleProfitPct.median(),
                    "meanNetReturnPct":hold.netReturnPct.mean(),"meanExcessVsPassivePct":hold.excessVsPassivePct.mean(),
                    "positiveCycleProfitPct":100*(hold.cycleProfitPct>0).mean()})
    pd.DataFrame(hold_rows).to_csv(OUT/"grid_range_research_holdout.csv", index=False)
    pd.DataFrame(errors).to_csv(OUT/"grid_range_research_data_issues.csv", index=False)
    best = summary.iloc[0] if not summary.empty else None
    lines = ["# Spot Grid research v2 — candle-based, research only", "",
      f"- Symbols with usable histories: {d.symbol.nunique()}",
      f"- 15-minute candle simulations: {len(d):,}",
      f"- Quote conversion is required for a 100 USDT-equivalent notional; pairs without a snapshot conversion are listed as excluded, not silently treated as USDT.",
      f"- Minimum order: {MIN_ORDER_USDT:.2f} USDT-equivalent; minimum grid spacing: {MIN_STEP_PCT*100:.2f}%; maximum grids: {MAX_GRIDS}.",
      f"- Fees assumed: {FEE*100:.2f}% per fill; parameters are based only on prior candles; test blocks are chronological 48-hour windows.",
      "- Primary ranking objective is simulated net profit from completed grid cycles after fees. Portfolio mark-to-market and excess versus passive 50/50 holding are separate diagnostics.",
      "- Same pair appears in multiple time blocks; the summary is not an independent-trial confidence interval. The later-time holdout is reported separately.",
      "- Intracandle high/low order sequencing is not reconstructed; simulation uses candle closes, so some fills are missed. This is research, not an exact Binance bot replay.", ""]
    if best is not None:
        lines += ["## Best family in aggregate sample", "",
          f"- Width: {best.widthSigma} × 48h volatility; lower/upper multipliers: {best.lowAsymmetry}/{best.highAsymmetry}.",
          f"- Grids/mode: {int(best.gridCount)} / {best.gridMode}.",
          f"- Mean cycle profit net of fees: {best.meanCycleProfitPct:.4f}% of capital per 48h window.",
          f"- Median cycle profit: {best.medianCycleProfitPct:.4f}%; median completed cycles: {best.medianCycles:.1f}.",
          f"- Mean total portfolio return: {best.meanNetReturnPct:.4f}%; mean excess vs passive: {best.meanExcessVsPassivePct:.4f} pp.", ""]
    lines += ["## Validation and data issues", "",
      f"- Later-time holdout rows: {len(hold_rows)}.",
      f"- Pair/data exclusions: {len(errors)} (see grid_range_research_data_issues.csv).",
      "- Do not deploy a configuration solely because it ranks first in the same sample; require positive later-block cycle profit and inspect mark-to-market losses.", ""]
    (OUT/"grid_range_research_summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
