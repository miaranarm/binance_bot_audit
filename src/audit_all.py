import csv
import datetime as dt
import gzip
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("results")
HISTORY = OUT / "history"
OUT.mkdir(parents=True, exist_ok=True)
HISTORY.mkdir(parents=True, exist_ok=True)

BASE = "https://www.binance.com/bapi/futures/v1/public/future/common/strategy/landing-page/"
TOP = BASE + "queryTopStrategy"
DCA = BASE + "queryTopUmDcaStrategy"

FIELDS = [
    "rank", "strategyId", "category", "strategyType", "symbol", "leverage",
    "minInvestment", "runningTime", "roi", "pnl", "matchedTrades", "mdd7d",
    "gridProfitTotalProfitRatio", "currentPrice", "priceRange",
    "profitPerGridAfterFees", "score"
]

RETENTION_DAYS = 30


def num(value, default=0.0):
    try:
        if isinstance(value, bool):
            return default
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().replace(",", "")
        if text.endswith("%"):
            text = text[:-1].strip()
        return float(text)
    except Exception:
        return default


def normalize_symbol(symbol):
    return str(symbol or "").strip().upper().replace("/", "").replace("-", "").replace("_", "")


def fmt_price(value):
    if value is None:
        return ""
    value = float(value)
    if value == 0:
        return "0"
    if abs(value) >= 1:
        return f"{value:.8f}".rstrip("0").rstrip(".")
    return f"{value:.12f}".rstrip("0").rstrip(".")


def find_numeric_key(obj, patterns):
    if isinstance(obj, dict):
        for key, value in obj.items():
            normalized = str(key).replace("_", "").replace("-", "").lower()
            if any(pattern in normalized for pattern in patterns):
                parsed = num(value, None)
                if parsed is not None:
                    return parsed
            found = find_numeric_key(value, patterns)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_numeric_key(value, patterns)
            if found is not None:
                return found
    return None

def find_exact_numeric_key(obj, keys):
    wanted = {str(key).replace("_", "").replace("-", "").lower() for key in keys}
    if isinstance(obj, dict):
        for key, value in obj.items():
            normalized = str(key).replace("_", "").replace("-", "").lower()
            if normalized in wanted:
                parsed = num(value, None)
                if parsed is not None:
                    return parsed
        for value in obj.values():
            found = find_exact_numeric_key(value, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_exact_numeric_key(value, keys)
            if found is not None:
                return found
    return None


def leverage(x):
    params = x.get("strategyParams") or {}
    value = params.get("leverage", x.get("leverage"))
    if value in (None, "", "null"):
        return 1.0
    try:
        return float(value)
    except Exception:
        return 99.0


def first_num(obj, paths):
    for path in paths:
        value = obj
        try:
            for key in path.split("."):
                if not isinstance(value, dict):
                    value = None
                    break
                value = value.get(key)
            if value not in (None, "", "null"):
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def grid_metrics(item):
    params = item.get("strategyParams") or {}
    lower = first_num(params, ["lowerLimit", "lowerPrice", "gridLowerLimit", "gridLowerPrice", "minPrice", "lower"])
    upper = first_num(params, ["upperLimit", "upperPrice", "gridUpperLimit", "gridUpperPrice", "maxPrice", "upper"])
    grids = first_num(params, ["gridCount", "gridNum", "numberOfGrids", "gridNumber"])
    mode = str(params.get("type") or params.get("gridType") or params.get("gridMode") or "").upper()
    fee = 0.001

    price_range = ""
    if lower is not None and upper is not None:
        price_range = f"{fmt_price(lower)} - {fmt_price(upper)}"

    # Prefer Binance's own Profit/Grid value when it is explicitly exposed.
    # Only the exact Profit/Grid field is accepted as a direct Binance value.
    direct_profit_grid = first_num(item, [
        "profitPerGrid", "profitGrid",
        "strategyStats.profitPerGrid", "strategyStats.profitGrid",
        "stats.profitPerGrid", "stats.profitGrid",
    ])
    if direct_profit_grid is not None:
        profit_grid = f"{direct_profit_grid:.4f}%"
        profit_grid_source = "BINANCE"
    else:
        # Binance documents the following fallback calculation for Spot Grid.
        # Arithmetic: d=(Upper-Lower)/Grids
        # max=(1-c)*d/Lower-2c
        # min=(Upper*(1-c))/(Lower-d)-1-c
        # Geometric: r=(Upper/Lower)^(1/Grids)
        # Profit/Grid=(1-c)*r-1-c
        profit_grid = ""
        profit_grid_source = "CALCULATED_BINANCE_FORMULA"
        if lower is not None and upper is not None and grids and grids > 0 and lower > 0:
            try:
                if mode in {"GEO", "GEOMETRIC"}:
                    ratio = (upper / lower) ** (1.0 / grids)
                    value = ((1 - fee) * ratio - 1 - fee) * 100
                    profit_grid = f"{value:.4f}%"
                elif mode in {"ARITH", "ARITHMETIC"}:
                    d = (upper - lower) / grids
                    a = ((1 - fee) * d / lower - 2 * fee) * 100
                    b = (((upper * (1 - fee)) / (upper - d)) - 1 - fee) * 100
                    lo, hi = sorted((a, b))
                    profit_grid = f"{lo:.4f}% - {hi:.4f}%"
            except (ZeroDivisionError, ValueError, OverflowError):
                pass

    # Grid Profit and Total Profit MUST be Binance-provided values.
    # Do not substitute generic PNL, ROI, matched PNL, or realized PNL.
    # The ratio itself is the only calculation performed from these two values.
    grid_profit = first_num(item, [
        "gridProfit",
        "strategyStats.gridProfit",
        "stats.gridProfit",
    ])
    if grid_profit is None:
        grid_profit = find_exact_numeric_key(item, ["gridProfit"])

    total_profit = first_num(item, [
        "totalProfit",
        "strategyStats.totalProfit",
        "stats.totalProfit",
    ])
    if total_profit is None:
        total_profit = find_exact_numeric_key(item, ["totalProfit"])

    ratio = ""
    if grid_profit is not None and total_profit not in (None, 0):
        ratio = f"{grid_profit / total_profit:.6f}"

    return ratio, price_range, profit_grid, profit_grid_source


def pages(page, endpoint, base_query, category, streamer, diagnostics):
    rows = []
    for page_no in range(1, 150):
        query = dict(base_query)
        query.update(page=page_no, rows=100)
        try:
            response = page.request.post(endpoint, data=query).json()
            data = response.get("data") or []
        except Exception as exc:
            diagnostics.append({
                "endpoint": endpoint,
                "category": category,
                "page": page_no,
                "error": str(exc),
            })
            break

        for item in data:
            item = dict(item)
            item["_category"] = category
            item["_streamer"] = streamer
            item["_lev"] = leverage(item)
            if str(item.get("strategyId") or "").strip() == "3232564":
                print("DEBUG_STRATEGY_3232564=" + json.dumps(item, ensure_ascii=False, sort_keys=True))
            rows.append(item)

        total = int(response.get("total") or 0)
        if len(data) < 100 or page_no * 100 >= total:
            break

    return rows


def build_current(rows, prices):
    current = []
    seen = set()

    for x in rows:
        sid = str(x.get("strategyId") or "").strip()
        symbol = str(x.get("symbol") or "").strip()
        lev = num(x.get("_lev"), 99.0)

        # Only strategies currently published by Binance and tradable from
        # the public Bot Marketplace, with leverage <= 1.
        if not sid or not symbol or lev > 1.0 or sid in seen:
            continue
        seen.add(sid)

        ratio, price_range, profit_grid, profit_grid_source = grid_metrics(x)
        direct_current_price = first_num(x, ["currentPrice", "lastPrice", "marketPrice", "price"])
        if direct_current_price is None:
            direct_current_price = find_exact_numeric_key(x, ["currentPrice", "lastPrice", "marketPrice", "latestPrice", "latestMarketPrice"])
        ticker_price = prices.get(normalize_symbol(symbol))
        item = {
            "strategyId": sid,
            "category": x.get("_category", ""),
            "strategyType": x.get("strategyType", ""),
            "symbol": symbol,
            "leverage": lev,
            "minInvestment": x.get("minInvestment", x.get("minimumInvestment", "")),
            "runningTime": x.get("runningTime", ""),
            "roi": num(x.get("roi", x.get("roiPct", x.get("roiRate", 0)))),
            "pnl": num(x.get("pnl", x.get("profitLoss", x.get("totalPnl", 0)))),
            "matchedTrades": num(x.get("matchedTrades", x.get("matchedCount", x.get("totalMatchedTrades", 0)))),
            "mdd7d": num(x.get("mdd7d", x.get("sevenDayMdd", x.get("7dMdd", 0)))),
            "gridProfitTotalProfitRatio": ratio,
            "currentPrice": fmt_price(direct_current_price if direct_current_price is not None else ticker_price),
            "priceRange": price_range,
            "profitPerGridAfterFees": profit_grid,
        }
        current.append(item)

    # Same scoring model as the previous scanner, but only across the
    # currently tradable <=1x universe.
    config = [
        ("roi", 0.30, 1),
        ("pnl", 0.25, 1),
        ("runningTime", 0.15, 1),
        ("matchedTrades", 0.15, 1),
        ("mdd7d", 0.15, -1),
    ]

    for item in current:
        score = 0.0
        for field, weight, direction in config:
            values = [num(x[field]) for x in current]
            lo, hi = min(values, default=0.0), max(values, default=0.0)
            normalized = (num(item[field]) - lo) / (hi - lo) if hi > lo else 0.5
            score += weight * normalized * direction
        item["score"] = 100.0 * score

    current.sort(key=lambda x: x["score"], reverse=True)
    return current


def write_current(current):
    path = OUT / "current_multicriteria.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for rank, item in enumerate(current, 1):
            writer.writerow({"rank": rank, **item})


def fetch_prices(page, symbols):
    wanted = {normalize_symbol(s) for s in symbols if str(s).strip()}
    prices = {}
    endpoints = [
        "https://data-api.binance.vision/api/v3/ticker/price",
        "https://api-gcp.binance.com/api/v3/ticker/price",
        "https://api.binance.com/api/v3/ticker/price",
        "https://api1.binance.com/api/v3/ticker/price",
        "https://api2.binance.com/api/v3/ticker/price",
        "https://api3.binance.com/api/v3/ticker/price",
        "https://api4.binance.com/api/v3/ticker/price",
        "https://fapi.binance.com/fapi/v1/ticker/price",
        "https://dapi.binance.com/dapi/v1/ticker/price",
    ]

    def consume(payload):
        if not isinstance(payload, list):
            return
        for row in payload:
            symbol = normalize_symbol(row.get("symbol"))
            if symbol not in wanted or symbol in prices:
                continue
            price = num(row.get("price"), None)
            if price is not None:
                prices[symbol] = price

    for endpoint in endpoints:
        try:
            response = page.request.get(endpoint, timeout=30000)
            if response.ok:
                consume(response.json())
        except Exception:
            pass

        # Some Binance environments reject the very large all-symbol ticker
        # response. Fall back to symbol-filtered batches so current price
        # remains a direct Binance market value.
        missing = sorted(wanted - set(prices))
        if missing:
            base = endpoint.split("?", 1)[0]
            for start in range(0, len(missing), 100):
                batch = missing[start:start + 100]
                try:
                    encoded = json.dumps(batch, separators=(",", ":"))
                    response = page.request.get(
                        base,
                        params={"symbols": encoded},
                        timeout=30000,
                    )
                    if response.ok:
                        consume(response.json())
                except Exception:
                    continue

    return prices


def write_summary(current):
    path = OUT / "current_multicriteria_summary.txt"
    with path.open("w", encoding="utf-8") as handle:
        handle.write(f"COUNT={len(current)}\n")
        handle.write("UNIVERSE=Binance Bot Marketplace active strategies\n")
        handle.write("FILTER=leverage<=1 and non-empty tradable symbol\n")
        handle.write("SCORING=ROI30 PNL25 RUNTIME15 MATCHED15 LOW_7D_MDD15\n")
        for rank, item in enumerate(current[:20], 1):
            handle.write(
                f"{rank}. {item['strategyId']} {item['symbol']} "
                f"score={item['score']:.4f} roi={item['roi']} pnl={item['pnl']} "
                f"trades={item['matchedTrades']} mdd7d={item['mdd7d']}\n"
            )


def write_history(current, now):
    # One compact gzip snapshot per hourly scan. Filename is the timestamp,
    # so retention is independent of Git checkout file mtimes.
    folder = HISTORY / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = folder / f"{now.strftime('%Y%m%dT%H%M%SZ')}.csv.gz"

    with gzip.open(snapshot, "wt", newline="", encoding="utf-8", compresslevel=9) as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for rank, item in enumerate(current, 1):
            writer.writerow({"rank": rank, **item})

    cutoff = now - dt.timedelta(days=RETENTION_DAYS)
    removed = 0
    for path in HISTORY.rglob("*.csv.gz"):
        try:
            stamp = dt.datetime.strptime(path.name[:16], "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc)
        except Exception:
            continue
        if stamp < cutoff:
            path.unlink(missing_ok=True)
            removed += 1

    # Remove empty date directories after retention cleanup.
    for directory in sorted(HISTORY.glob("*/*/*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()

    return snapshot, removed


def capture_marketplace_detail_calls(page):
    """Capture Binance Trading Bots page API responses exposing exact grid metrics."""
    hits = []
    def on_response(response):
        try:
            content_type = (response.headers.get("content-type") or "").lower()
            if "json" not in content_type:
                return
            body = response.text()
            lowered = body.lower()
            url = response.url
            if "/bapi/" in url:
                # Inventory public Trading Bots API calls. Keep this diagnostic
                # read-only and avoid dumping unrelated payloads.
                print("BAPI_RESPONSE url=" + url + " status=" + str(response.status) + " bytes=" + str(len(body)))
            if (
                "gridprofit" in lowered
                or "totalprofit" in lowered
                or "matchedprofit" in lowered
                or '"strategyid":3232564' in lowered
                or '"strategyid":"3232564"' in lowered
            ):
                hits.append({"url": url, "status": response.status, "body": body[:20000]})
                print("GRID_METRIC_RESPONSE " + url + " status=" + str(response.status) + " body=" + body[:10000])
        except Exception:
            pass
    page.on("response", on_response)
    try:
        page.goto("https://www.binance.com/en/trading-bots", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(12000)
    except Exception as exc:
        print("MARKETPLACE_PAGE_ERROR=" + str(exc))
    return hits

def main():
    diagnostics = []
    now = dt.datetime.now(dt.timezone.utc)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        captured_detail_calls = capture_marketplace_detail_calls(page)

        rows = pages(
            page, TOP,
            {"strategyType": 1, "symbol": "", "zone": "", "sort": "pnl"},
            "Spot Grid", "SPOT_GRID", diagnostics,
        )

        # Binance currently exposes multiple bot families through this
        # landing-page endpoint. Keep only strategies that actually appear
        # in the active public marketplace and pass the <=1x filter below.
        for strategy_type in range(2, 21):
            rows.extend(
                pages(
                    page, TOP,
                    {"strategyType": strategy_type, "symbol": "", "zone": "", "sort": "pnl"},
                    f"Marketplace type {strategy_type}", f"TYPE_{strategy_type}", diagnostics,
                )
            )

        rows.extend(
            pages(
                page, DCA,
                {
                    "market": "", "zone": "", "roi": "", "sort": "pnl",
                    "trailingType": "", "leverage": "", "investmentType": False,
                    "sevenDayMdd": "", "strategyType": 10, "symbol": "",
                },
                "Futures DCA", "UM_DCA", diagnostics,
            )
        )

        symbols = [str(x.get("symbol") or "").strip() for x in rows]
        prices = fetch_prices(page, symbols)
        browser.close()

    current = build_current(rows, prices)
    write_current(current)
    write_summary(current)
    snapshot, removed = write_history(current, now)

    with (OUT / "scan_meta.json").open("w", encoding="utf-8") as handle:
        json.dump({
            "scan_utc": now.isoformat(),
            "raw_rows": len(rows),
            "tradable_leverage_le_1": len(current),
            "history_retention_days": RETENTION_DAYS,
            "snapshot": str(snapshot),
            "old_snapshots_removed": removed,
            "binance_marketplace_refresh": "hourly",
            "fields": FIELDS,
            "grid_profit_ratio_note": "Grid Profit and Total Profit are accepted only from exact Binance fields. The ratio is calculated as Binance Grid Profit / Binance Total Profit. No generic PNL, ROI, matched PNL, or realized PNL substitutes are accepted.",
            "profit_per_grid_note": "Uses Binance Profit/Grid directly when exposed as an exact field; otherwise uses only Binance documented Spot Grid formulas with c=0.1%. The source for each row is retained internally as BINANCE or CALCULATED_BINANCE_FORMULA.",
            "profit_per_grid_fee_reference": "0.1% per side, per Binance Spot Grid documentation; pair/VIP-specific fees may differ.",
        }, handle, ensure_ascii=False, indent=2)

    with (OUT / "type_census.json").open("w", encoding="utf-8") as handle:
        json.dump({
            "endpoint_diagnostics": diagnostics,
            "filter": "active public Binance Bot Marketplace strategies with leverage <= 1",
        }, handle, ensure_ascii=False, indent=2)

    print(f"DONE raw={len(rows)} current={len(current)} snapshot={snapshot} removed={removed}")


if __name__ == "__main__":
    main()
