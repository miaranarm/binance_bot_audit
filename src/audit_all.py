import csv
import datetime as dt
import gzip
import json
import os
from pathlib import Path
from urllib.parse import quote

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
    "gridProfit", "gridProfitSource", "totalProfit", "totalProfitSource", "gridProfitTotalProfitRatio", "gridProfitTotalProfitRatioSource", "currentPrice", "priceRange",
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


def estimate_grid_profit(item, total_profit):
    """Best-effort reconstruction when Binance exposes neither Grid Profit nor
    the ratio itself.

    For Spot Grid, Binance defines marketplace PNL as total profit and defines
    Grid Profit as the sum of matched buy/sell pairs after fees.  We can
    reconstruct an auditable estimate from the published grid geometry,
    matched-trade count, ROI and PNL.  ROI gives the strategy investment
    (PNL / ROI), while Binance's grid model gives the price levels.  We then
    infer
    the per-order quantity from the investment required by the currently
    active buy/sell levels and multiply the average net grid profit by the
    matched-trade count.

    This is deliberately marked ESTIMATED: the public marketplace does not
    expose the exact matched-order history/quantities, so an exact value is
    impossible from marketplace fields alone.
    """
    if str(item.get("_category") or "").lower() != "spot grid":
        return None

    params = item.get("strategyParams") or {}

    def pnum(paths):
        value = first_num(params, paths)
        if value is not None:
            return value
        return first_num(item, paths)

    lower = pnum(["lowerLimit", "lowerPrice", "gridLowerLimit", "gridLowerPrice", "minPrice", "lower"])
    upper = pnum(["upperLimit", "upperPrice", "gridUpperLimit", "gridUpperPrice", "maxPrice", "upper"])
    grids = pnum(["gridCount", "gridNum", "numberOfGrids", "gridNumber"])
    current = pnum(["currentPrice", "lastPrice", "marketPrice", "price", "_audit_current_price"])
    matched = num(item.get("matchedTrades", item.get("matchedCount", item.get("totalMatchedTrades", 0))), 0.0)
    roi = num(item.get("roi", item.get("roiPct", item.get("roiRate", 0))), 0.0)
    fee = 0.001

    if (lower is None or upper is None or grids is None or current is None or
            grids < 2 or lower <= 0 or upper <= lower or matched <= 0 or
            total_profit in (None, 0) or roi == 0):
        return None

    try:
        grids_i = int(round(grids))
        if grids_i < 2:
            return None

        # Marketplace ROI = PNL / Investment. This gives the investment
        # scale without relying on the minimum-investment field.
        # Binance defines marketplace ROI as PNL / Investment. The
        # marketplace displays ROI in percent, hence the /100 conversion.
        investment = float(total_profit) / (float(roi) / 100.0)
        if investment <= 0 or not (investment == investment):
            return None

        raw_mode = params.get("type", params.get("gridType", params.get("gridMode", "")))
        mode = str(raw_mode).strip().upper()
        # Binance payloads can encode grid mode as either a label or a
        # numeric enum. Keep the mapping explicit; never silently treat an
        # unknown mode as geometric.
        if mode in {"2", "GEO", "GEOMETRIC"}:
            geometric = True
        elif mode in {"1", "ARITH", "ARITHMETIC"}:
            geometric = False
        else:
            return None

        if geometric:
            step = (upper / lower) ** (1.0 / grids_i)
            levels = [lower * (step ** i) for i in range(grids_i + 1)]
        else:
            step = (upper - lower) / grids_i
            levels = [lower + step * i for i in range(grids_i + 1)]

        # Binance leaves the grid level immediately surrounding the current
        # price empty; lower levels are buys and higher levels are sells.
        below = [p for p in levels if p < current]
        above = [p for p in levels if p > current]
        if not below or not above:
            return None

        # If price is exactly on a grid level, do not count that level as an
        # active order. This keeps the active order count aligned with the
        # documented buy/sell placement model.
        buy_prices = below
        sell_prices = above
        active_value_per_qty = sum(buy_prices) + len(sell_prices) * current
        if active_value_per_qty <= 0:
            return None

        qty = investment / active_value_per_qty

        cycle_profits = []
        for i in range(grids_i):
            buy = levels[i]
            sell = levels[i + 1]
            gross = (sell - buy) * qty
            fees = fee * (sell + buy) * qty
            net = gross - fees
            if net > 0:
                cycle_profits.append(net)

        if not cycle_profits:
            return None

        estimated_grid_profit = matched * (sum(cycle_profits) / len(cycle_profits))
        return estimated_grid_profit
    except (ZeroDivisionError, ValueError, OverflowError):
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

    direct_profit_grid = first_num(item, [
        "profitPerGrid", "profitGrid",
        "strategyStats.profitPerGrid", "strategyStats.profitGrid",
        "stats.profitPerGrid", "stats.profitGrid",
    ])
    if direct_profit_grid is not None:
        profit_grid = f"{direct_profit_grid:.4f}%"
        profit_grid_source = "BINANCE"
    else:
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

    # First choice: exact Binance Grid Profit and Total Profit.
    grid_profit = first_num(item, [
        "_detail_grid_profit",
        "gridProfit",
        "strategyStats.gridProfit",
        "stats.gridProfit",
    ])
    if grid_profit is None:
        grid_profit = find_exact_numeric_key(item, ["gridProfit"])

    # Binance defines Spot Grid Total Profit = Grid Profit + Floating/Unrealized
    # PnL. If the public payload exposes the floating component but omits
    # Grid Profit, reconstruct Grid Profit exactly from those two published
    # values. This is preferable to any geometry-based estimate.
    floating_pnl = first_num(item, [
        "_detail_floating_pnl",
        "floatingPnl",
        "floatingPNL",
        "unrealizedPnl",
        "unrealizedPNL",
        "floatProfit",
        "floatingProfit",
        "strategyStats.floatingPnl",
        "strategyStats.unrealizedPnl",
        "stats.floatingPnl",
        "stats.unrealizedPnl",
    ])
    if floating_pnl is None:
        floating_pnl = find_exact_numeric_key(
            item, ["floatingPnl", "unrealizedPnl", "floatProfit", "floatingProfit"]
        )

    total_profit = first_num(item, [
        "_detail_total_profit",
        "totalProfit",
        "strategyStats.totalProfit",
        "stats.totalProfit",
    ])
    if total_profit is None:
        total_profit = find_exact_numeric_key(item, ["totalProfit"])

    # Binance's marketplace PNL is Total Profit for Spot Grid. Resolve that
    # denominator BEFORE attempting the exact reconstruction so that a detail
    # response exposing only Floating/Unrealized PnL can still yield:
    # Grid Profit = Total Profit - Floating/Unrealized PnL.
    marketplace_pnl = num(item.get("pnl"), None)
    if (str(item.get("_category") or "").lower() == "spot grid"
            and total_profit in (None, 0)
            and marketplace_pnl not in (None, 0)):
        total_profit = marketplace_pnl

    # Exact reconstruction path:
    # Binance defines Total Profit = Grid Profit + Unrealized PnL.
    # Therefore, when Binance gives Total Profit (directly or via the
    # marketplace PNL field) and Floating/Unrealized PnL, the missing
    # Grid Profit is recovered exactly; no grid-geometry estimate is needed.
    if grid_profit is None and total_profit is not None and floating_pnl is not None:
        grid_profit = total_profit - floating_pnl
        grid_profit_source = "BINANCE_TOTAL_PROFIT_MINUS_FLOATING_PNL"
    else:
        detail_source = str(item.get("_detail_metric_source") or "")
        if grid_profit is not None and detail_source == "BINANCE_DETAIL_VISIBLE":
            grid_profit_source = "BINANCE_DETAIL_VISIBLE_GRID_PROFIT"
        elif grid_profit is not None and detail_source == "BINANCE_DETAIL_API":
            grid_profit_source = "BINANCE_DETAIL_API_GRID_PROFIT"
        elif grid_profit is not None and detail_source == "BINANCE_QUERY_ROI_CHART":
            grid_profit_source = "BINANCE_QUERY_ROI_CHART_GRID_PROFIT"
        else:
            grid_profit_source = "BINANCE_GRID_PROFIT" if grid_profit is not None else ""

    ratio = ""
    ratio_source = "UNAVAILABLE"

    # Do not use the geometry/matched-trade reconstruction as the official
    # Grid Profit. Binance calculates Grid Profit from the actual matched
    # buy/sell executions and their fees; marketplace aggregates do not expose
    # those executions. Keep that estimator for research only.
    #
    # Exact Grid Profit may legitimately exceed Total Profit when the
    # Floating/Unrealized PnL is negative because Binance defines:
    # Total Profit = Grid Profit + Unrealized PnL.
    if grid_profit is not None and total_profit not in (None, 0):
        ratio = f"{grid_profit / total_profit:.6f}"
        if grid_profit_source == "BINANCE_TOTAL_PROFIT_MINUS_FLOATING_PNL":
            ratio_source = "BINANCE_TOTAL_PROFIT_MINUS_FLOATING_PNL_DIV_TOTAL_PROFIT"
        elif total_profit == marketplace_pnl and marketplace_pnl not in (None, 0):
            ratio_source = "BINANCE_GRID_PROFIT_DIV_MARKETPLACE_PNL"
        else:
            ratio_source = "BINANCE_GRID_PROFIT_DIV_TOTAL_PROFIT"

    return ratio, price_range, profit_grid, profit_grid_source, ratio_source, grid_profit, total_profit, grid_profit_source


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

        direct_current_price = first_num(x, ["currentPrice", "lastPrice", "marketPrice", "price"])
        if direct_current_price is None:
            direct_current_price = find_exact_numeric_key(x, ["currentPrice", "lastPrice", "marketPrice", "latestPrice", "latestMarketPrice"])
        ticker_price = prices.get(normalize_symbol(symbol))
        x["_audit_current_price"] = direct_current_price if direct_current_price is not None else ticker_price
        ratio, price_range, profit_grid, profit_grid_source, ratio_source, grid_profit, total_profit, grid_profit_source = grid_metrics(x)
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
            "gridProfit": "" if grid_profit is None else grid_profit,
            "gridProfitSource": grid_profit_source,
            "totalProfit": "" if total_profit is None else total_profit,
            "totalProfitSource": (
                "BINANCE_DETAIL_VISIBLE_TOTAL_PROFIT" if x.get("_detail_metric_source") == "BINANCE_DETAIL_VISIBLE" and total_profit is not None
                else "BINANCE_DETAIL_API_TOTAL_PROFIT" if x.get("_detail_metric_source") == "BINANCE_DETAIL_API" and total_profit is not None
                else "BINANCE_QUERY_ROI_CHART_TOTAL_PROFIT" if x.get("_detail_metric_source") == "BINANCE_QUERY_ROI_CHART" and total_profit is not None
                else "BINANCE_TOTAL_PROFIT" if first_num(x, ["_detail_total_profit", "totalProfit", "strategyStats.totalProfit", "stats.totalProfit"]) is not None
                else "BINANCE_MARKETPLACE_PNL_AS_TOTAL_PROFIT" if total_profit is not None else ""
            ),
            "gridProfitTotalProfitRatio": ratio,
            "gridProfitTotalProfitRatioSource": ratio_source,
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


def capture_marketplace_detail_calls(page, rows):
    """Capture exact Binance Grid Profit/Total Profit from public detail APIs."""
    metrics_by_sid = {}
    active_sid = {"value": None}
    debug_path = Path(__file__).resolve().parent.parent / "strategy_detail_debug.log"
    debug_path.parent.mkdir(parents=True, exist_ok=True)
    debug_path.write_text("DETAIL_CAPTURE_START\n", encoding="utf-8")
    print("DETAIL_CAPTURE_START " + str(debug_path))

    def debug_write(line):
        try:
            with debug_path.open("a", encoding="utf-8") as handle:
                handle.write(str(line)[:50000] + "\n")
        except Exception:
            pass

    def parse_visible_metric(body_text, label):
        import re
        text_value = str(body_text or "").replace("\n", " | ")
        pattern = str(label) + r"\s*\|\s*(-?\d[\d,]*(?:\.\d+)?)"
        match = re.search(pattern, text_value, flags=re.IGNORECASE)
        if not match:
            return None
        return num(match.group(1).replace(",", ""), None)

    def on_response(response):
        try:
            content_type = (response.headers.get("content-type") or "").lower()
            if "json" not in content_type:
                return
            body = response.text()
            lowered = body.lower()
            url = response.url
            if "/bapi/" in url:
                print("BAPI_RESPONSE url=" + url + " status=" + str(response.status) + " bytes=" + str(len(body)))
            if "/api/v2/query" in url.lower() or "/api/v1/feature-gate/check" in url.lower() or "/api/v2/strategy/query" in url.lower():
                debug_write("DETAIL_SERVICE_BODY url=" + url + " status=" + str(response.status) + " body=" + body[:50000])
                print("DETAIL_SERVICE_BODY url=" + url + " status=" + str(response.status) + " bytes=" + str(len(body)))
                if "/api/v2/strategy/query" in url.lower():
                    try:
                        request = response.request
                        debug_write("DETAIL_SERVICE_REQUEST url=" + url + " method=" + str(request.method) + " post=" + str(request.post_data or ""))
                        print("DETAIL_SERVICE_REQUEST url=" + url + " method=" + str(request.method))
                    except Exception as exc:
                        debug_write("DETAIL_SERVICE_REQUEST_ERROR " + str(exc))

            # queryRoiChart is a public Binance landing-page endpoint that is
            # called with the real marketplace strategyId. Capture its complete
            # payload for diagnostics and extract any exact profit fields if
            # Binance exposes them there.
            if "/strategy/landing-page/queryroichart" in url.lower():
                debug_write("ROI_CHART_BODY sid=" + str(active_sid["value"]) +
                            " url=" + url + " body=" + body[:50000])
                try:
                    payload = json.loads(body)
                    roi_grid = find_exact_numeric_key(payload, ["gridProfit"])
                    roi_total = find_exact_numeric_key(payload, ["totalProfit"])
                    roi_float = find_exact_numeric_key(
                        payload, ["floatingPnl", "unrealizedPnl", "floatProfit", "floatingProfit"]
                    )
                    if roi_grid is not None or roi_total is not None or roi_float is not None:
                        entry = metrics_by_sid.setdefault(active_sid["value"], {})
                        if roi_grid is not None:
                            entry["gridProfit"] = roi_grid
                        if roi_total is not None:
                            entry["totalProfit"] = roi_total
                        if roi_float is not None:
                            entry["floatingPnl"] = roi_float
                        entry["source"] = "BINANCE_QUERY_ROI_CHART"
                        entry["url"] = url
                        debug_write("ROI_CHART_EXACT sid=" + str(active_sid["value"]) +
                                    " gridProfit=" + str(roi_grid) +
                                    " totalProfit=" + str(roi_total) +
                                    " floatingPnl=" + str(roi_float))
                except Exception as exc:
                    debug_write("ROI_CHART_PARSE_ERROR sid=" + str(active_sid["value"]) + " " + str(exc))
            # Only accept exact Binance fields. Never infer metrics from PNL/ROI.
            if active_sid["value"] and any(token in lowered for token in (
                "gridprofit", "totalprofit", "floatingpnl", "unrealizedpnl", "floatprofit", "floatingprofit"
            )):
                try:
                    payload = json.loads(body)
                    grid_profit = find_exact_numeric_key(payload, ["gridProfit"])
                    total_profit = find_exact_numeric_key(payload, ["totalProfit"])
                    floating_pnl = find_exact_numeric_key(
                        payload, ["floatingPnl", "unrealizedPnl", "floatProfit", "floatingProfit"]
                    )
                    if grid_profit is not None or total_profit is not None or floating_pnl is not None:
                        entry = metrics_by_sid.setdefault(active_sid["value"], {})
                        if grid_profit is not None:
                            entry["gridProfit"] = grid_profit
                        if total_profit is not None:
                            entry["totalProfit"] = total_profit
                        if floating_pnl is not None:
                            entry["floatingPnl"] = floating_pnl
                        entry["source"] = "BINANCE_DETAIL_API"
                        entry["url"] = url
                        debug_write("EXACT_GRID_METRICS sid=" + str(active_sid["value"]) +
                                    " gridProfit=" + str(grid_profit) +
                                    " totalProfit=" + str(total_profit) +
                                    " floatingPnl=" + str(floating_pnl) +
                                    " url=" + url)
                        print("EXACT_GRID_METRICS sid=" + str(active_sid["value"]) +
                              " gridProfit=" + str(grid_profit) +
                              " totalProfit=" + str(total_profit) +
                              " floatingPnl=" + str(floating_pnl))
                except Exception:
                    pass
            if any(token in url.lower() for token in ("/grid/", "strategy/detail", "strategy/info", "strategy/landing-page/")):
                print("DETAIL_API_RESPONSE " + url + " status=" + str(response.status) + " body=" + body[:12000])
        except Exception:
            pass

    page.on("response", on_response)
    def on_request(request):
        try:
            url = request.url
            low = url.lower()
            if any(token in low for token in ("strategy", "detail", "grid")):
                debug_write("DETAIL_REQUEST url=" + url + " method=" + str(request.method) + " post=" + str(request.post_data or ""))
        except Exception as exc:
            debug_write("DETAIL_REQUEST_ERROR " + str(exc))
    page.on("request", on_request)
    try:
        page.goto("https://www.binance.com/en/trading-bots", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(10000)

        candidates = []
        seen = set()
        for row in rows:
            sid = str(row.get("strategyId") or "").strip()
            symbol = str(row.get("symbol") or "").strip()
            category = str(row.get("_category") or "").lower()
            if not sid or not symbol or sid in seen:
                continue
            if "spot grid" in category:
                path = "https://www.binance.com/en/trading-bots/spot/grid/detail"
            elif "futures grid" in category:
                path = "https://www.binance.com/en/trading-bots/futures/grid/detail"
            else:
                continue

            # Exclude pending/placeholder strategies. Their detail page is
            # rendered as "Pending Trigger" and reports 0/-- by design.
            running = num(row.get("runningTime"), 0.0)
            matched = num(row.get("matchedCount", row.get("latestMatchedCount", 0)), 0.0)
            pnl = num(row.get("pnl"), 0.0)
            if running <= 0 or matched <= 0 or pnl == 0:
                continue

            seen.add(sid)
            candidates.append((path, symbol, sid, row.get("_category"), running, matched, pnl))

        # Deterministic sample of ACTIVE Grid strategies only. No symbol or
        # strategy ID is privileged.
        # Prefer the newest strategy IDs. Older public marketplace rows can
        # remain visible long after their detail page becomes a Pending Trigger
        # placeholder, so high activity/matched-count alone is not sufficient.
        candidates.sort(key=lambda item: (-int(item[2]) if str(item[2]).isdigit() else 0, item[2]))
        spot = [x for x in candidates if "spot grid" in str(x[3]).lower()][:8]
        futures = [x for x in candidates if "futures grid" in str(x[3]).lower()][:8]
        candidates = spot + futures

        for path, symbol, sid, category, running, matched, pnl in candidates:
            active_sid["value"] = sid
            url = path + "?symbol=" + quote(str(symbol)) + "&strategyId=" + quote(str(sid))
            print("DETAIL_PAGE " + str(category) + " " + url)
            debug_write("DETAIL_PAGE " + str(category) + " " + url)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)
                resources = page.evaluate("() => performance.getEntriesByType('resource').map(x => x.name).filter(Boolean)")
                for resource_url in resources:
                    lowered_resource = str(resource_url).lower()
                    if any(token in lowered_resource for token in ("strategy", "grid", "detail", "profit")):
                        print("DETAIL_RESOURCE " + resource_url)
                html = page.content()
                lowered_html = html.lower()
                metric_tokens = ("gridprofit", "totalprofit", "matchedprofit", "realizedprofit", "unrealizedpnl", "floatingprofit")
                if any(token in lowered_html for token in metric_tokens):
                    print("DETAIL_HTML_METRICS " + url)
                    for token in metric_tokens:
                        pos = lowered_html.find(token)
                        if pos >= 0:
                            print("DETAIL_HTML_CONTEXT " + token + " " + html[max(0, pos-800):pos+1800])
                # The detail page may expose Grid Profit / Total Profit only as
                # rendered text rather than JSON fields. Capture the visible
                # text around those labels so we can bind exact values without
                # substituting marketplace PNL/ROI.
                try:
                    body_text = page.locator("body").inner_text(timeout=10000)
                    visible_grid = parse_visible_metric(body_text, "Grid Profit")
                    visible_total = parse_visible_metric(body_text, "Total Profit")
                    visible_float = parse_visible_metric(body_text, "Floating Profit")
                    pending_detail = ("Pending Trigger" in body_text or "Duration --" in body_text)
                    if pending_detail:
                        debug_write("DETAIL_VISIBLE_REJECTED_PENDING sid=" + str(sid))
                        print("DETAIL_VISIBLE_REJECTED_PENDING sid=" + str(sid))
                        visible_grid = None
                        visible_total = None
                        visible_float = None
                    if visible_grid is not None or visible_total is not None or visible_float is not None:
                        entry = metrics_by_sid.setdefault(sid, {})
                        if visible_grid is not None:
                            entry["gridProfit"] = visible_grid
                        if visible_total is not None:
                            entry["totalProfit"] = visible_total
                        if visible_float is not None:
                            entry["floatingPnl"] = visible_float
                        entry["source"] = "BINANCE_DETAIL_VISIBLE"
                        entry["url"] = url
                        debug_write("DETAIL_VISIBLE_EXACT sid=" + str(sid) +
                                    " gridProfit=" + str(visible_grid) +
                                    " totalProfit=" + str(visible_total) +
                                    " floatingPnl=" + str(visible_float))
                        print("DETAIL_VISIBLE_EXACT sid=" + str(sid) +
                              " gridProfit=" + str(visible_grid) +
                              " totalProfit=" + str(visible_total) +
                              " floatingPnl=" + str(visible_float))
                    for label in ("Grid Profit", "Total Profit", "Floating Profit"):
                        pos = body_text.lower().find(label.lower())
                        if pos >= 0:
                            context = body_text[max(0, pos-300):pos+700].replace("\n", " | ")
                            debug_write("DETAIL_VISIBLE_METRIC sid=" + str(sid) + " label=" + label + " context=" + context)
                            print("DETAIL_VISIBLE_METRIC sid=" + str(sid) + " label=" + label + " context=" + context)
                except Exception as exc:
                    debug_write("DETAIL_VISIBLE_METRIC_ERROR sid=" + str(sid) + " " + str(exc))
            except Exception as exc:
                print("DETAIL_PAGE_ERROR " + url + " " + str(exc))
    except Exception as exc:
        print("MARKETPLACE_PAGE_ERROR=" + str(exc))
    active_sid["value"] = None
    return metrics_by_sid

def main():
    diagnostics = []
    now = dt.datetime.now(dt.timezone.utc)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

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

        debug_root = Path(__file__).resolve().parent.parent / "strategy_detail_debug.log"
        debug_root.write_text("MAIN_REACHED_DETAIL_CALL\\n", encoding="utf-8")
        print("MAIN_REACHED_DETAIL_CALL " + str(debug_root))
        captured_detail_metrics = capture_marketplace_detail_calls(page, rows)
        if captured_detail_metrics:
            for row in rows:
                sid = str(row.get("strategyId") or "").strip()
                metrics = captured_detail_metrics.get(sid)
                if metrics:
                    row["_detail_grid_profit"] = metrics.get("gridProfit")
                    row["_detail_total_profit"] = metrics.get("totalProfit")
                    row["_detail_floating_pnl"] = metrics.get("floatingPnl")
                    row["_detail_metric_source"] = metrics.get("source", "")

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
            "grid_profit_ratio_note": "Priority is exact Binance detail data: visible Detail-page Grid Profit/Total Profit, detail API fields, then the public ROI-chart payload. If Binance exposes Total Profit plus Floating/Unrealized PnL but omits Grid Profit, exact Grid Profit is reconstructed as Total Profit - Floating/Unrealized PnL. No ratio is capped at 100%; a ratio above 1 can be mathematically valid when Floating/Unrealized PnL is negative. No geometry/matched-trade estimate is used as the official metric.",
            "profit_per_grid_note": "Uses Binance Profit/Grid directly when exposed as an exact field; otherwise uses Binance Spot Grid formulas with c=0.1%. A range is intentional for arithmetic grids because the same absolute grid step produces a different percentage return at each price level; geometric grids normally produce one percentage. The value is the net profit of one completed buy/sell grid cycle after fees, not the bot ROI.",
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