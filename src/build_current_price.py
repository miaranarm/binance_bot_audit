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
API = "https://api.binance.com/api/v3/klines"
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
    """Monthly candles retain exact intramonth lows/highs; Binance began in 2017."""
    params = urlencode({"symbol": symbol, "interval": "1M", "limit": 1000})
    request = Request(API + "?" + params, headers={"User-Agent": "binance-bot-audit/1.0"})
    for attempt in range(4):
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, list) or not payload:
                return symbol, None, None, "NO_KLINES"
            lows = [float(candle[3]) for candle in payload if len(candle) > 3]
            highs = [float(candle[2]) for candle in payload if len(candle) > 2]
            if not lows or not highs:
                return symbol, None, None, "NO_KLINES"
            return symbol, min(lows), max(highs), "OK"
        except HTTPError as exc:
            # Invalid/delisted symbols should not repeatedly stall the audit.
            if exc.code in (400, 404):
                return symbol, None, None, f"HTTP_{exc.code}"
            if exc.code == 429 or exc.code >= 500:
                time.sleep(1.5 * (attempt + 1))
            else:
                return symbol, None, None, f"HTTP_{exc.code}"
        except (URLError, TimeoutError, ValueError, json.JSONDecodeError):
            time.sleep(1.0 * (attempt + 1))
    return symbol, None, None, "REQUEST_FAILED"


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
    listing["symbol"] = listing["symbol"].astype(str).str.strip()
    listing["checkedDateUTC"] = listing["checkedDateUTC"].fillna("").astype(str)
    listing = listing.drop_duplicates("symbol", keep="last")

    active_symbols = sorted(set(now["symbol"]))
    cached = listing.set_index("symbol", drop=False).to_dict("index") if not listing.empty else {}
    to_query = [symbol for symbol in active_symbols
                if symbol not in cached or cached[symbol].get("checkedDateUTC", "") != today]
    queried = {}
    if to_query:
        # Moderate parallelism while remaining within Binance's public API rate limits.
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(get_binance_listing_extrema, symbol): symbol for symbol in to_query}
            for future in as_completed(futures):
                symbol, low, high, status = future.result()
                queried[symbol] = {
                    "symbol": symbol, "binancePriceLow": low, "binancePriceHigh": high,
                    "status": status, "checkedDateUTC": today
                }

    records = []
    for symbol in sorted(set(list(cached) | set(active_symbols))):
        old = cached.get(symbol, {})
        new = queried.get(symbol)
        if new and new["status"] == "OK":
            # Monthly low/high can extend as the current month develops.
            record = new
        elif new:
            # Keep last known valid values on temporary API failures / delisted pairs.
            record = {
                "symbol": symbol,
                "binancePriceLow": old.get("binancePriceLow"),
                "binancePriceHigh": old.get("binancePriceHigh"),
                "status": new["status"],
                "checkedDateUTC": today
            }
        else:
            record = {
                "symbol": symbol,
                "binancePriceLow": old.get("binancePriceLow"),
                "binancePriceHigh": old.get("binancePriceHigh"),
                "status": old.get("status", ""),
                "checkedDateUTC": old.get("checkedDateUTC", "")
            }
        records.append(record)
    listing = pd.DataFrame(records, columns=[
        "symbol", "binancePriceLow", "binancePriceHigh", "status", "checkedDateUTC"])
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
    failures = int(listing.loc[listing["symbol"].isin(active_symbols), "status"].ne("OK").sum())
    print(f"{len(result)} paires -> {OUTPUT}; Binance listing extrema checked today: {len(to_query)} queried, {failures} active symbols without a fresh valid result.")


if __name__ == "__main__":
    main()
