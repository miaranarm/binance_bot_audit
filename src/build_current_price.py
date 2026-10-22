#!/usr/bin/env python3
"""Build pair-level prices, project extrema, and Binance listing-to-date extrema."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
import time
import pandas as pd

RESULTS = Path("results")
CURRENT = RESULTS / "current_multicriteria.csv"
PROJECT_TRACKER = RESULTS / "current_price_history.csv"
BINANCE_TRACKER = RESULTS / "binance_listing_extrema.csv"
OUTPUT = RESULTS / "current_price.csv"
HISTORY = RESULTS / "history"
APIS = [
    "https://data-api.binance.vision/api/v3/klines",
    "https://api.binance.com/api/v3/klines",
    "https://api-gcp.binance.com/api/v3/klines",
    "https://api1.binance.com/api/v3/klines",
    "https://api2.binance.com/api/v3/klines",
]
OUT_COLUMNS = [
    "symbol", "minInvestment", "currentPrice",
    "priceLow", "priceHigh", "binancePriceLow", "binancePriceHigh", "Niveau"
]


def num(series):
    return pd.to_numeric(series, errors="coerce")


def read_snapshot(path):
    try:
        frame = pd.read_csv(path, compression="gzip", usecols=lambda c: c in {"symbol", "currentPrice"})
        if {"symbol", "currentPrice"}.issubset(frame.columns):
            frame["symbol"] = frame["symbol"].astype(str).str.strip()
            frame["currentPrice"] = num(frame["currentPrice"])
            return frame[["symbol", "currentPrice"]].dropna()
    except Exception:
        pass
    return pd.DataFrame(columns=["symbol", "currentPrice"])


def get_binance_listing_extrema(symbol):
    """Accept listing extrema only when monthly and complete daily history agree."""
    last_status = "REQUEST_FAILED"
    for api in APIS:
        try:
            def fetch(interval, start_time=None):
                params = {"symbol": symbol, "interval": interval, "limit": 1000}
                if start_time is not None:
                    params["startTime"] = int(start_time)
                request = Request(api + "?" + urlencode(params), headers={"User-Agent": "binance-bot-audit/1.0"})
                with urlopen(request, timeout=12) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, list):
                    raise ValueError("Invalid klines response")
                return payload

            def checked(payload):
                rows = []
                for c in payload:
                    if len(c) < 7:
                        raise ValueError("Incomplete kline")
                    t = int(c[0]); o, h, l, close = map(float, (c[1], c[2], c[3], c[4]))
                    if not all(v > 0 and v < float("inf") for v in (o, h, l, close)):
                        raise ValueError("Invalid OHLC value")
                    if l > min(o, close) or h < max(o, close) or l > h:
                        raise ValueError("Inconsistent OHLC candle")
                    rows.append((t, l, h))
                return rows

            monthly = checked(fetch("1M"))
            if not monthly:
                last_status = "NO_KLINES"
                continue
            daily_low, daily_high = float("inf"), 0.0
            start_time, previous_start, pages = monthly[0][0], -1, 0
            while True:
                payload = fetch("1d", start_time)
                daily = checked(payload)
                if not daily:
                    break
                if daily[0][0] <= previous_start:
                    raise ValueError("Daily pagination did not advance")
                previous_start = daily[-1][0]
                daily_low = min(daily_low, min(row[1] for row in daily))
                daily_high = max(daily_high, max(row[2] for row in daily))
                pages += 1
                if len(payload) < 1000:
                    break
                if pages >= 20:
                    raise ValueError("Daily history exceeds safety page limit")
                start_time = previous_start + 86400000
            if daily_low == float("inf") or daily_high <= 0:
                last_status = "NO_DAILY_KLINES"
                continue
            monthly_low = min(row[1] for row in monthly)
            monthly_high = max(row[2] for row in monthly)
            low_ok = abs(monthly_low - daily_low) <= max(monthly_low, daily_low) * 1e-6
            high_ok = abs(monthly_high - daily_high) <= max(monthly_high, daily_high) * 1e-6
            if not (low_ok and high_ok):
                return symbol, None, None, "INTERVAL_MISMATCH"
            return symbol, daily_low, daily_high, "OK_DAILY_VERIFIED"
        except HTTPError as exc:
            last_status = f"HTTP_{exc.code}"
            if exc.code in (429, 500, 502, 503, 504):
                time.sleep(0.8)
        except (URLError, TimeoutError, ValueError, json.JSONDecodeError):
            last_status = "INVALID_KLINE_DATA"
    return symbol, None, None, last_status

def main():
    current = pd.read_csv(CURRENT, dtype={"symbol": str})
    required = {"symbol", "minInvestment", "currentPrice", "Niveau"}
    missing = sorted(required - set(current.columns))
    if missing:
        raise SystemExit(f"Colonnes absentes de {CURRENT}: {missing}")
    current["symbol"] = current["symbol"].astype(str).str.strip()
    current["minInvestment"] = num(current["minInvestment"])
    current["currentPrice"] = num(current["currentPrice"])
    current["Niveau"] = num(current["Niveau"])
    current = current[current["symbol"].ne("") & current["symbol"].notna()].copy()
    if current.empty:
        raise SystemExit("Aucune paire exploitable dans current_multicriteria.csv")

    now = current[["symbol", "currentPrice"]].dropna()
    now = now[now["currentPrice"].gt(0)]

    # Projet history: exact extrema of the prices captured by this project's hourly snapshots.
    if PROJECT_TRACKER.exists():
        prior = pd.read_csv(PROJECT_TRACKER, dtype={"symbol": str})
        prior["symbol"] = prior["symbol"].astype(str).str.strip()
        prior["priceLow"] = num(prior["priceLow"])
        prior["priceHigh"] = num(prior["priceHigh"] if "priceHigh" in prior else prior.get("pruceHigh"))
        current_extrema = now.groupby("symbol", as_index=False).agg(
            observedLow=("currentPrice", "min"), observedHigh=("currentPrice", "max"))
        extrema = prior[["symbol", "priceLow", "priceHigh"]].merge(
            current_extrema, on="symbol", how="outer")
        extrema["priceLow"] = pd.concat([extrema["priceLow"], extrema["observedLow"]], axis=1).min(axis=1)
        extrema["priceHigh"] = pd.concat([extrema["priceHigh"], extrema["observedHigh"]], axis=1).max(axis=1)
        extrema = extrema[["symbol", "priceLow", "priceHigh"]]
    else:
        frames = [read_snapshot(path) for path in HISTORY.rglob("*.csv.gz")]
        frames = [frame for frame in frames if not frame.empty]
        observations = pd.concat(frames + [now], ignore_index=True) if frames else now
        extrema = observations.groupby("symbol", as_index=False).agg(
            priceLow=("currentPrice", "min"), priceHigh=("currentPrice", "max"))
    extrema.to_csv(PROJECT_TRACKER, index=False)

    # Binance listing-to-date extremes: query monthly OHLC candles once per UTC day per symbol.
    # Monthly candles include each month's actual low/high, without downloading every daily candle.
    today = datetime.now(timezone.utc).date().isoformat()
    if BINANCE_TRACKER.exists():
        listing = pd.read_csv(BINANCE_TRACKER, dtype={"symbol": str})
        if "checkedDateUTC" not in listing:
            listing["checkedDateUTC"] = ""
    else:
        listing = pd.DataFrame(columns=[
            "symbol", "binancePriceLow", "binancePriceHigh", "status", "checkedDateUTC"])
    for column in ("binancePriceLow", "binancePriceHigh"):
        listing[column] = num(listing[column]) if column in listing else float("nan")
    if "validationMethod" not in listing:
        listing["validationMethod"] = ""
    listing["symbol"] = listing["symbol"].astype(str).str.strip()
    listing["checkedDateUTC"] = listing["checkedDateUTC"].fillna("").astype(str)
    listing = listing.drop_duplicates("symbol", keep="last")

    active_symbols = sorted(set(now["symbol"]))
    cached = listing.set_index("symbol", drop=False).to_dict("index") if not listing.empty else {}
    # Retry any non-OK symbol on each audit; only successful extrema are cached daily.
    # Otherwise a temporary HTTP 451/403 would suppress fallback-host retries until tomorrow.
    to_query = [symbol for symbol in active_symbols
                if symbol not in cached
                or cached[symbol].get("checkedDateUTC", "") != today
                or cached[symbol].get("status", "") != "OK_DAILY_VERIFIED"
                or cached[symbol].get("validationMethod", "") != "monthly-vs-full-daily-v1"]
    queried = {}
    if to_query:
        # Moderate parallelism while remaining within Binance's public API rate limits.
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(get_binance_listing_extrema, symbol): symbol for symbol in to_query}
            for future in as_completed(futures):
                symbol, low, high, status = future.result()
                queried[symbol] = {
                    "symbol": symbol, "binancePriceLow": low, "binancePriceHigh": high,
                    "status": status, "checkedDateUTC": today,
                    "validationMethod": "monthly-vs-full-daily-v1"
                }

    records = []
    for symbol in sorted(set(cached) | set(active_symbols)):
        old = cached.get(symbol, {})
        new = queried.get(symbol)
        if new and new["status"] == "OK_DAILY_VERIFIED":
            record = new
        elif new:
            trusted_old = old.get("validationMethod", "") == "monthly-vs-full-daily-v1" and old.get("status") == "OK_DAILY_VERIFIED"
            record = {
                "symbol": symbol,
                "binancePriceLow": old.get("binancePriceLow") if trusted_old else None,
                "binancePriceHigh": old.get("binancePriceHigh") if trusted_old else None,
                "status": new["status"],
                "checkedDateUTC": today,
                "validationMethod": "monthly-vs-full-daily-v1"
            }
        else:
            record = {
                "symbol": symbol,
                "binancePriceLow": old.get("binancePriceLow"),
                "binancePriceHigh": old.get("binancePriceHigh"),
                "status": old.get("status", ""),
                "checkedDateUTC": old.get("checkedDateUTC", ""),
                "validationMethod": old.get("validationMethod", "")
            }
        records.append(record)
    listing = pd.DataFrame(records, columns=[
        "symbol", "binancePriceLow", "binancePriceHigh", "status", "checkedDateUTC", "validationMethod"])
    listing.to_csv(BINANCE_TRACKER, index=False)

    # One row per pair; use the bot with the lowest minimum investment to select Niveau.
    current = current.sort_values(["symbol", "minInvestment"], ascending=[True, True],
                                  na_position="last", kind="stable")
    pair_rows = current.drop_duplicates("symbol", keep="first")[
        ["symbol", "minInvestment", "currentPrice", "Niveau"]]
    result = pair_rows.merge(extrema, on="symbol", how="left").merge(
        listing[["symbol", "binancePriceLow", "binancePriceHigh"]], on="symbol", how="left")
    result = result[OUT_COLUMNS].sort_values("symbol", kind="stable")
    temp = OUTPUT.with_suffix(".csv.tmp")
    result.to_csv(temp, index=False)
    temp.replace(OUTPUT)
    failures = int(listing.loc[listing["symbol"].isin(active_symbols), "status"].ne("OK_DAILY_VERIFIED").sum())
    print(f"{len(result)} paires -> {OUTPUT}; full daily-history validation: {len(to_query)} queried, {failures} active symbols without verified extrema.")


if __name__ == "__main__":
    main()
