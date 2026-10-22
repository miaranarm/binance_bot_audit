#!/usr/bin/env python3
"""Diagnose unusually distant Binance Spot extrema and record the exact daily candles.

This is a diagnostic only: it never changes current_price.csv or listing extrema.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import csv
import json
import math
import time

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "results" / "current_price.csv"
OUTPUT = ROOT / "diagnostics" / "extrema_candle_diagnostic.csv"
APIS = [
    "https://data-api.binance.vision/api/v3/klines",
    "https://api.binance.com/api/v3/klines",
    "https://api-gcp.binance.com/api/v3/klines",
]
QUOTE_SUFFIXES = ("USDT", "USDC", "FDUSD", "BUSD", "TUSD", "USDP", "DAI", "USD1")
KNOWN_CHECKS = {"LINKUSDT", "ICPUSDT", "GLMRUSDT", "ATOMUSDT", "BNBUSDT"}
LIMIT = 1000
MAX_PAGES = 20


def finite_positive(value):
    try:
        number = float(value)
        return number > 0 and math.isfinite(number)
    except (TypeError, ValueError):
        return False


def fetch_page(symbol, start_time):
    last_error = "REQUEST_FAILED"
    for api in APIS:
        for attempt in range(2):
            try:
                params = {"symbol": symbol, "interval": "1d", "limit": LIMIT}
                if start_time is not None:
                    params["startTime"] = start_time
                req = Request(api + "?" + urlencode(params),
                              headers={"User-Agent": "binance-bot-audit-extrema-diagnostic/1.0"})
                with urlopen(req, timeout=15) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, list):
                    raise ValueError("NON_LIST_RESPONSE")
                return payload
            except HTTPError as exc:
                last_error = f"HTTP_{exc.code}"
                if exc.code in (429, 500, 502, 503, 504):
                    time.sleep(0.6 * (attempt + 1))
                else:
                    break
            except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                last_error = type(exc).__name__
                time.sleep(0.3 * (attempt + 1))
    raise RuntimeError(last_error)


def candles_for(symbol):
    start_time = 0
    page_count = 0
    lowest = None
    highest = None
    seen_last = -1
    while page_count < MAX_PAGES:
        page = fetch_page(symbol, start_time)
        if not page:
            break
        page_count += 1
        for row in page:
            if len(row) < 7:
                raise RuntimeError("INCOMPLETE_KLINE")
            timestamp = int(row[0])
            opening, high, low, close = map(float, (row[1], row[2], row[3], row[4]))
            if not all(math.isfinite(v) and v > 0 for v in (opening, high, low, close)):
                raise RuntimeError("INVALID_OHLC")
            if low > min(opening, close) or high < max(opening, close) or low > high:
                raise RuntimeError("INCONSISTENT_OHLC")
            candle = (timestamp, opening, high, low, close)
            if lowest is None or low < lowest[3]:
                lowest = candle
            if highest is None or high > highest[2]:
                highest = candle
        last_open = int(page[-1][0])
        if last_open <= seen_last:
            raise RuntimeError("PAGINATION_DID_NOT_ADVANCE")
        seen_last = last_open
        if len(page) < LIMIT:
            break
        start_time = last_open + 86_400_000
    else:
        raise RuntimeError("MAX_PAGES_REACHED")
    if lowest is None or highest is None:
        raise RuntimeError("NO_DAILY_KLINES")
    return lowest, highest, page_count


def iso_date(timestamp_ms):
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).date().isoformat()


def candle_fields(prefix, candle):
    timestamp, opening, high, low, close = candle
    return {
        prefix + "DateUTC": iso_date(timestamp),
        prefix + "Open": f"{opening:.16g}",
        prefix + "High": f"{high:.16g}",
        prefix + "Low": f"{low:.16g}",
        prefix + "Close": f"{close:.16g}",
    }


def diagnose(row):
    symbol = row["symbol"].strip()
    current = float(row["currentPrice"])
    stored_low = float(row["binancePriceLow"])
    stored_high = float(row["binancePriceHigh"])
    try:
        low_candle, high_candle, pages = candles_for(symbol)
        low_wick_ratio = low_candle[3] / min(low_candle[1], low_candle[4])
        high_wick_ratio = high_candle[2] / max(high_candle[1], high_candle[4])
        low_match = math.isclose(low_candle[3], stored_low, rel_tol=1e-8, abs_tol=1e-14)
        high_match = math.isclose(high_candle[2], stored_high, rel_tol=1e-8, abs_tol=1e-14)
        flags = []
        if not low_match:
            flags.append("STORED_LOW_MISMATCH")
        if not high_match:
            flags.append("STORED_HIGH_MISMATCH")
        if low_wick_ratio <= 0.1:
            flags.append("LOW_WICK_10X_OR_MORE")
        if high_wick_ratio >= 10:
            flags.append("HIGH_WICK_10X_OR_MORE")
        if not flags:
            flags.append("NO_RULE_TRIGGERED")
        result = {
            "symbol": symbol,
            "status": "OK",
            "currentPrice": row["currentPrice"],
            "storedLow": row["binancePriceLow"],
            "storedHigh": row["binancePriceHigh"],
            "lowRatioToCurrent": f"{stored_low / current:.12g}",
            "highRatioToCurrent": f"{stored_high / current:.12g}",
            "dailyLow": f"{low_candle[3]:.16g}",
            "dailyHigh": f"{high_candle[2]:.16g}",
            "lowWickRatio": f"{low_wick_ratio:.12g}",
            "highWickRatio": f"{high_wick_ratio:.12g}",
            "pagesRead": pages,
            "flags": ";".join(flags),
            **candle_fields("lowCandle", low_candle),
            **candle_fields("highCandle", high_candle),
            "error": "",
        }
        return result
    except Exception as exc:
        return {
            "symbol": symbol, "status": "ERROR", "currentPrice": row["currentPrice"],
            "storedLow": row["binancePriceLow"], "storedHigh": row["binancePriceHigh"],
            "lowRatioToCurrent": "", "highRatioToCurrent": "", "dailyLow": "",
            "dailyHigh": "", "lowWickRatio": "", "highWickRatio": "", "pagesRead": "",
            "flags": "NOT_VERIFIED", "lowCandleDateUTC": "", "lowCandleOpen": "",
            "lowCandleHigh": "", "lowCandleLow": "", "lowCandleClose": "",
            "highCandleDateUTC": "", "highCandleOpen": "", "highCandleHigh": "",
            "highCandleLow": "", "highCandleClose": "", "error": str(exc),
        }


def main():
    if not INPUT.exists():
        raise SystemExit(f"Missing input: {INPUT}")
    with INPUT.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    candidates = []
    for row in rows:
        symbol = row.get("symbol", "").strip()
        if not symbol or not symbol.endswith(QUOTE_SUFFIXES):
            continue
        if not all(finite_positive(row.get(key)) for key in
                   ("currentPrice", "binancePriceLow", "binancePriceHigh")):
            continue
        current = float(row["currentPrice"])
        low_ratio = float(row["binancePriceLow"]) / current
        high_ratio = float(row["binancePriceHigh"]) / current
        if symbol in KNOWN_CHECKS or low_ratio <= 0.01 or high_ratio >= 20:
            candidates.append(row)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "symbol", "status", "currentPrice", "storedLow", "storedHigh",
        "lowRatioToCurrent", "highRatioToCurrent", "dailyLow", "dailyHigh",
        "lowWickRatio", "highWickRatio", "pagesRead", "flags",
        "lowCandleDateUTC", "lowCandleOpen", "lowCandleHigh", "lowCandleLow", "lowCandleClose",
        "highCandleDateUTC", "highCandleOpen", "highCandleHigh", "highCandleLow", "highCandleClose",
        "error",
    ]
    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(diagnose, row): row["symbol"] for row in candidates}
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item["symbol"])
    with OUTPUT.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)
    flagged = sum(result["status"] != "OK" or result["flags"] != "NO_RULE_TRIGGERED"
                  for result in results)
    print(f"Diagnostic pairs: {len(results)}; flagged/error rows: {flagged}; output: {OUTPUT}")


if __name__ == "__main__":
    main()
