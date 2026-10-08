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
    "gridProfit", "gridProfitSource", "gridMode", "gridCount", "qtyPerOrderEstimate", "qtyPerOrderSource", "gridProfitEstimateLow", "gridProfitEstimateMid", "gridProfitEstimateHigh",
    "totalProfit", "totalProfitSource", "gridProfitTotalProfitRatio", "gridProfitTotalProfitRatioSource",
    "gridProfitTotalProfitRatioEstimateLow", "gridProfitTotalProfitRatioEstimateMid", "gridProfitTotalProfitRatioEstimateHigh", "gridProfitTotalProfitRatioStatus",
    "floatingProfit", "gridProfitEstimateMethod", "gridProfitConfidence", "gridProfitEstimateStatus", "floatingProfitSource",
    "currentPrice", "priceRange", "profitPerGridAfterFees", "score"
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


def _quote_asset(symbol):
    symbol = normalize_symbol(symbol)
    quotes = ("USDT", "USDC", "FDUSD", "TUSD", "USDP", "BUSD", "DAI", "BTC", "ETH", "BNB", "EUR", "TRY", "BRL")
    for quote in quotes:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return quote
    return ""


def _quote_usd_price(symbol, prices):
    quote = _quote_asset(symbol)
    if not quote:
        return None
    if quote in {"USDT", "USDC", "FDUSD", "TUSD", "USDP", "BUSD", "DAI"}:
        return 1.0
    direct = prices.get(normalize_symbol(quote + "USDT"))
    if direct:
        return direct
    inverse = prices.get(normalize_symbol("USDT" + quote))
    if inverse:
        return 1.0 / inverse
    return None


def _grid_mode(params):
    raw = params.get("type", params.get("gridType", params.get("gridMode", "")))
    mode = str(raw).strip().upper()
    if mode in {"2", "GEO", "GEOMETRIC"}:
        return "GEOMETRIC"
    if mode in {"1", "ARITH", "ARITHMETIC"}:
        return "ARITHMETIC"
    return ""


def _grid_levels(lower, upper, grids, mode):
    if mode == "GEOMETRIC":
        ratio = (upper / lower) ** (1.0 / grids)
        return [lower * (ratio ** i) for i in range(grids + 1)]
    if mode == "ARITHMETIC":
        step = (upper - lower) / grids
        return [lower + step * i for i in range(grids + 1)]
    return []


def _roi_interval(raw_roi):
    """Return (low, midpoint, high) for a displayed/rounded ROI."""
    try:
        raw = str(raw_roi).strip().replace(",", "")
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value == 0:
        return None
    if "." in raw:
        decimals = len(raw.split(".", 1)[1])
        step = 10 ** (-decimals)
    else:
        step = 1.0
    half = step / 2.0
    return max(value - half, 1e-12), value, value + half


def estimate_grid_profit(item, total_profit_usd, prices):
    """Estimate Grid Profit and Grid/Total ratio with explicit uncertainty."""
    if str(item.get("_category") or "").lower() != "spot grid":
        return None

    params = item.get("strategyParams") or {}
    def pnum(paths):
        value = first_num(params, paths)
        return value if value is not None else first_num(item, paths)

    lower = pnum(["lowerLimit", "lowerPrice", "gridLowerLimit", "gridLowerPrice", "minPrice", "lower"])
    upper = pnum(["upperLimit", "upperPrice", "gridUpperLimit", "gridUpperPrice", "maxPrice", "upper"])
    grids = pnum(["gridCount", "gridNum", "numberOfGrids", "gridNumber"])
    current = pnum(["currentPrice", "lastPrice", "marketPrice", "price", "_audit_current_price"])
    matched = num(item.get("matchedTrades", item.get("matchedCount", item.get("totalMatchedTrades", 0))), 0.0)
    roi_interval = _roi_interval(item.get("roi", item.get("roiPct", item.get("roiRate", 0))))

    if (lower is None or upper is None or grids is None or current is None or
            grids < 2 or lower <= 0 or upper <= lower or matched <= 0 or
            total_profit_usd in (None, 0) or roi_interval is None):
        return None

    try:
        grids_i = int(round(grids))
        mode = _grid_mode(params)
        if grids_i < 2 or not mode:
            return None

        roi_low, roi_mid, roi_high = roi_interval
        investment_low = float(total_profit_usd) / (roi_high / 100.0)
        investment_mid = float(total_profit_usd) / (roi_mid / 100.0)
        investment_high = float(total_profit_usd) / (roi_low / 100.0)
        if min(investment_low, investment_mid, investment_high) <= 0:
            return None

        quote_usd = _quote_usd_price(str(item.get("symbol") or ""), prices)
        if quote_usd is None or quote_usd <= 0:
            return None

        levels = _grid_levels(lower, upper, grids_i, mode)
        below = [p for p in levels if p < current]
        above = [p for p in levels if p > current]
        if not below or not above:
            return None

        allocation_per_qty = sum(below) + len(above) * current
        denominator = allocation_per_qty * 1.001
        if denominator <= 0:
            return None

        def qty_for_investment(investment_usd):
            return (investment_usd / quote_usd) / denominator

        qty_low = qty_for_investment(investment_low)
        qty_mid = qty_for_investment(investment_mid)
        qty_high = qty_for_investment(investment_high)

        cycle_low = []
        cycle_mid = []
        cycle_high = []
        for i in range(grids_i):
            buy, sell = levels[i], levels[i + 1]
            net_per_qty = (sell - buy) - 0.001 * (buy + sell)
            cycle_low.append(max(0.0, net_per_qty * qty_low))
            cycle_mid.append(max(0.0, net_per_qty * qty_mid))
            cycle_high.append(max(0.0, net_per_qty * qty_high))

        low_quote = matched * min(cycle_low)
        high_quote = matched * max(cycle_high)
        mid_quote = matched * (sum(cycle_mid) / len(cycle_mid))

        raw_roi = str(item.get("roi", "")).strip()
        precision = len(raw_roi.split(".", 1)[1]) if "." in raw_roi else 0
        confidence = "LOW" if roi_mid < 0.1 or precision <= 1 else "MEDIUM"

        return {
            "low": low_quote * quote_usd,
            "high": high_quote * quote_usd,
            "mid": mid_quote * quote_usd,
            "mode": mode,
            "investmentUsd": investment_mid,
            "investmentLowUsd": investment_low,
            "investmentHighUsd": investment_high,
            "quoteUsd": quote_usd,
            "method": "BINANCE_FORMULA_RECONSTRUCTION_ROUNDED_ROI_AND_GRID_RANGE",
            "confidence": confidence,
            "gridMode": mode,
            "gridCount": grids_i,
            "qtyPerOrderEstimate": qty_mid,
            "qtyPerOrderLow": qty_low,
            "qtyPerOrderHigh": qty_high,
            "qtyPerOrderSource": "RECONSTRUCTED_FROM_ROUNDED_ROI_AND_GRID_STATE",
        }
    except (ZeroDivisionError, ValueError, OverflowError):
        return None

def grid_metrics(item, prices):
    params = item.get("strategyParams") or {}
    lower = first_num(params, ["lowerLimit", "lowerPrice", "gridLowerLimit", "gridLowerPrice", "minPrice", "lower"])
    upper = first_num(params, ["upperLimit", "upperPrice", "gridUpperLimit", "gridUpperPrice", "maxPrice", "upper"])
    grids = first_num(params, ["gridCount", "gridNum", "numberOfGrids", "gridNumber"])
    mode = _grid_mode(params)
    fee = 0.001

    price_range = ""
    if lower is not None and upper is not None:
        price_range = f"{fmt_price(lower)} - {fmt_price(upper)}"

    # Binance's documented Profit/Grid formula is exact from grid geometry.
    # Arithmetic grids genuinely have a range: the percentage return differs
    # at each absolute price level. Geometric grids have one fixed percentage.
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
        if lower is not None and upper is not None and grids and grids > 0 and lower > 0 and mode:
            try:
                if mode == "GEOMETRIC":
                    ratio = (upper / lower) ** (1.0 / grids)
                    value = ((1 - fee) * ratio - 1 - fee) * 100
                    profit_grid = f"{value:.4f}%"
                elif mode == "ARITHMETIC":
                    d = (upper - lower) / grids
                    maximum = ((1 - fee) * d / lower - 2 * fee) * 100
                    minimum = (((upper * (1 - fee)) / (upper - d)) - 1 - fee) * 100
                    lo, hi = sorted((minimum, maximum))
                    profit_grid = f"{lo:.4f}% - {hi:.4f}%"
            except (ZeroDivisionError, ValueError, OverflowError):
                pass

    marketplace_pnl = num(item.get("pnl"), None)

    detail_grid = first_num(item, ["_detail_grid_profit", "gridProfit", "strategyStats.gridProfit", "stats.gridProfit"])
    if detail_grid is None:
        detail_grid = find_exact_numeric_key(item, ["gridProfit"])

    detail_total = first_num(item, ["_detail_total_profit", "totalProfit", "strategyStats.totalProfit", "stats.totalProfit"])
    if detail_total is None:
        detail_total = find_exact_numeric_key(item, ["totalProfit"])

    floating_pnl = first_num(item, [
        "_detail_floating_pnl", "floatingPnl", "floatingPNL", "unrealizedPnl",
        "unrealizedPNL", "floatProfit", "floatingProfit",
        "strategyStats.floatingPnl", "strategyStats.unrealizedPnl",
        "stats.floatingPnl", "stats.unrealizedPnl",
    ])
    if floating_pnl is None:
        floating_pnl = find_exact_numeric_key(
            item, ["floatingPnl", "unrealizedPnl", "floatProfit", "floatingProfit"]
        )

    # Marketplace PNL is explicitly displayed in USD. For Spot Grid it is
    # Total Profit, i.e. Current Value - Initial Investment. Keep this as the
    # canonical totalProfit unit so every row is comparable.
    if str(item.get("_category") or "").lower() == "spot grid" and marketplace_pnl is not None:
        total_profit = marketplace_pnl
        total_source = "BINANCE_MARKETPLACE_PNL_USD_AS_TOTAL_PROFIT"
    elif detail_total is not None:
        total_profit = detail_total
        total_source = "BINANCE_DETAIL_TOTAL_PROFIT"
    else:
        total_profit = None
        total_source = ""

    grid_profit = None
    grid_profit_source = ""
    ratio = ""
    ratio_source = "UNAVAILABLE"
    ratio_estimate_low = ratio_estimate_mid = ratio_estimate_high = None
    ratio_status = "UNAVAILABLE"
    estimate_low = estimate_high = None
    estimate_method = ""
    estimate_confidence = ""
    estimate_grid_mode = ""
    estimate_grid_count = None
    estimate_qty = None
    estimate_qty_source = ""

    # Best path: Binance exposes Grid Profit and Total Profit from the same
    # detail payload. Their ratio is exact and unit-independent. We scale the
    # exact ratio by the marketplace USD Total Profit so gridProfit is also
    # comparable in USD across all pairs.
    exact_total = detail_total
    if exact_total is None and detail_grid is not None and floating_pnl is not None:
        exact_total = detail_grid + floating_pnl
    if detail_grid is not None and exact_total not in (None, 0):
        ratio_value = detail_grid / exact_total
        ratio = f"{ratio_value:.6f}"
        ratio_source = "BINANCE_DETAIL_GRID_DIV_TOTAL_EXACT"
        ratio_status = "EXACT"
        if total_profit is not None:
            grid_profit = total_profit * ratio_value
            grid_profit_source = "BINANCE_EXACT"
        else:
            grid_profit = detail_grid
            grid_profit_source = "BINANCE_EXACT"
        estimate_low = estimate_high = grid_profit
        estimate_method = "BINANCE_EXACT_DETAIL"
        estimate_confidence = "HIGH"

    # Second exact path: Total Profit + Floating Profit => Grid Profit.
    if grid_profit is None and detail_total not in (None, 0) and floating_pnl is not None:
        detail_grid_reconstructed = detail_total - floating_pnl
        if total_profit is not None:
            ratio_value = detail_grid_reconstructed / detail_total
            grid_profit = total_profit * ratio_value
            ratio = f"{ratio_value:.6f}"
            ratio_source = "BINANCE_DETAIL_TOTAL_MINUS_FLOATING_DIV_TOTAL_EXACT"
            ratio_status = "EXACT"
            grid_profit_source = "BINANCE_EXACT"
        else:
            grid_profit = detail_grid_reconstructed
            grid_profit_source = "BINANCE_EXACT"
        estimate_low = estimate_high = grid_profit
        estimate_method = "BINANCE_EXACT_DETAIL_RECONSTRUCTION"
        estimate_confidence = "HIGH"

    # No exact Grid Profit: reconstruct a bounded estimate from the documented
    # grid mechanics. This is not hidden as exact; the interval is persisted.
    if grid_profit is None and total_profit not in (None, 0):
        estimate = estimate_grid_profit(item, total_profit, prices)
        if estimate:
            estimate_low = estimate["low"]
            estimate_mid = estimate["mid"]
            estimate_high = estimate["high"]
            grid_profit = estimate_mid
            grid_profit_source = "RECONSTRUCTED"
            estimate_method = estimate.get("method", "BINANCE_FORMULA_RECONSTRUCTION")
            estimate_confidence = estimate.get("confidence", "MEDIUM")
            estimate_grid_mode = estimate.get("gridMode", "")
            estimate_grid_count = estimate.get("gridCount")
            estimate_qty = estimate.get("qtyPerOrderEstimate")
            estimate_qty_source = estimate.get("qtyPerOrderSource", "")
            ratio_estimate_low = estimate_low / total_profit if total_profit else None
            ratio_estimate_mid = estimate_mid / total_profit if total_profit else None
            ratio_estimate_high = estimate_high / total_profit if total_profit else None
            ratio_source = "RECONSTRUCTED_GRID_PROFIT_DIV_BINANCE_MARKETPLACE_TOTAL_PROFIT"
            ratio_status = "ESTIMATED_NOT_EXACT"
            ratio = "" if ratio_estimate_mid is None else f"{ratio_estimate_mid:.6f}"

    # gridProfit remains reserved for Binance-exact detail data.
    # The principal Grid/Total ratio remains populated from the central
    # reconstruction and is explicitly marked ESTIMATED_NOT_EXACT.

    floating_profit = None
    floating_basis = None
    if total_profit is not None and grid_profit is not None:
        floating_profit = total_profit - grid_profit
        floating_basis = "BINANCE_EXACT_GRID_PROFIT"

    return (
        ratio, price_range, profit_grid, profit_grid_source, ratio_source,
        ratio_estimate_low, ratio_estimate_mid, ratio_estimate_high, ratio_status,
        grid_profit, total_profit, grid_profit_source, estimate_low, estimate_mid,
        estimate_high, total_source, floating_profit, floating_basis,
        estimate_method, estimate_confidence, estimate_grid_mode, estimate_grid_count, estimate_qty, estimate_qty_source
    )

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
        ratio, price_range, profit_grid, profit_grid_source, ratio_source, ratio_estimate_low, ratio_estimate_mid, ratio_estimate_high, ratio_status, grid_profit, total_profit, grid_profit_source, estimate_low, estimate_mid, estimate_high, total_source, floating_profit, floating_basis, estimate_method, estimate_confidence, estimate_grid_mode, estimate_grid_count, estimate_qty, estimate_qty_source = grid_metrics(x, prices)
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
            "gridMode": estimate_grid_mode,
            "gridCount": "" if estimate_grid_count is None else estimate_grid_count,
            "qtyPerOrderEstimate": "" if estimate_qty is None else estimate_qty,
            "qtyPerOrderSource": estimate_qty_source,
            "gridProfitEstimateLow": "" if estimate_low is None else estimate_low,
            "gridProfitEstimateMid": "" if estimate_mid is None else estimate_mid,
            "gridProfitEstimateHigh": "" if estimate_high is None else estimate_high,
            "totalProfit": "" if total_profit is None else total_profit,
            "totalProfitSource": total_source,
            "gridProfitTotalProfitRatio": ratio,
            "gridProfitTotalProfitRatioSource": ratio_source,
            "gridProfitTotalProfitRatioEstimateLow": "" if ratio_estimate_low is None else ratio_estimate_low,
            "gridProfitTotalProfitRatioEstimateMid": "" if ratio_estimate_mid is None else ratio_estimate_mid,
            "gridProfitTotalProfitRatioEstimateHigh": "" if ratio_estimate_high is None else ratio_estimate_high,
            "gridProfitTotalProfitRatioStatus": ratio_status,
            "floatingProfit": "" if floating_profit is None else floating_profit,
            "floatingProfitSource": floating_basis or "",
            "gridProfitEstimateMethod": estimate_method,
            "gridProfitConfidence": estimate_confidence,
            "gridProfitEstimateStatus": ("EXACT" if grid_profit_source == "BINANCE_EXACT" else ("ESTIMATED_NOT_EXACT" if grid_profit_source == "RECONSTRUCTED" else "")),
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