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

# Diagnostic only: public Binance responses may expose capital fields under
# names that differ between Marketplace endpoints. Do not use these fields
# in the CSV until their accounting meaning and units are verified.
CAPITAL_FIELD_NAMES = {
    "totalinvestment", "initialinvestment", "investmentamount",
    "investedamount", "investmentusd", "investmentvalue",
    "initialcapital", "totalcapital", "capitalinvested",
    "initialmargin", "startinvestment", "mininvestment",
    "minimuminvestment", "investment", "totalinvest",
}
CAPITAL_FIELD_DIAGNOSTICS = []
PUBLIC_BAPI_ENDPOINT_SCHEMAS = []
PUBLIC_BAPI_ENDPOINT_SEEN = set()


def summarize_json_schema(payload, path="$", depth=0, max_depth=3, paths=None):
    """Compact schema-only summary; never stores full public API response bodies."""
    if paths is None:
        paths = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            child = f"{path}.{key}"
            if len(paths) < 100:
                paths.append(child)
            if depth < max_depth and isinstance(value, (dict, list)):
                summarize_json_schema(value, child, depth + 1, max_depth, paths)
    elif isinstance(payload, list) and payload:
        summarize_json_schema(payload[0], f"{path}[0]", depth + 1, max_depth, paths)
    return paths


def collect_capital_fields(payload, path="$"):
    """Return candidate capital/investment fields with JSON paths and raw values."""
    found = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized = str(key).replace("_", "").replace("-", "").lower()
            if normalized in CAPITAL_FIELD_NAMES:
                if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                    found.append({"path": f"{path}.{key}", "key": str(key), "value": value})
                elif value is None:
                    found.append({"path": f"{path}.{key}", "key": str(key), "value": None})
            found.extend(collect_capital_fields(value, f"{path}.{key}"))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(collect_capital_fields(value, f"{path}[{index}]"))
    return found

# --- collecte : paramètres de robustesse -------------------------------------
PAGE_SIZE = 100
MAX_PAGES = 400            # 40 000 lignes par catégorie (l'ancienne limite était 14 900)
MAX_ATTEMPTS = 4           # nouvelles tentatives par page, attente 1 s, 2 s, 4 s...


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
        # Spot Grid is unleveraged by design; unknown leverage for other families fails closed.
        return 1.0 if "spot grid" in str(x.get("_category", "")).lower() else 99.0
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

        # Ne pas tronquer les profits nets négatifs à zéro : les frais peuvent
        # dépasser le pas de grille. Comme les bornes d'investissement et les
        # niveaux de grille interagissent, calculer les bornes sur toutes les
        # combinaisons extrêmes au lieu d'assumer que qty_low donne toujours
        # le profit le plus faible.
        net_per_qty = [
            (levels[i + 1] - levels[i]) - 0.001 * (levels[i] + levels[i + 1])
            for i in range(grids_i)
        ]
        possible = [
            matched * net * qty
            for net in net_per_qty
            for qty in (qty_low, qty_mid, qty_high)
        ]
        low_quote = min(possible)
        high_quote = max(possible)
        mid_quote = matched * qty_mid * (sum(net_per_qty) / len(net_per_qty))

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
    estimate_low = estimate_mid = estimate_high = None
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
        estimate_low = estimate_mid = estimate_high = grid_profit
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
        estimate_low = estimate_mid = estimate_high = grid_profit
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

    # Reconstructed Grid Profit is intentionally promoted into the CSV, but
    # remains explicitly marked RECONSTRUCTED / ESTIMATED_NOT_EXACT.

    # Floating/unrealized PnL is only derivable when Grid Profit is exact
    # and Total Profit uses the same Binance accounting basis. Subtracting an
    # estimated Grid Profit from Total Profit would create a misleading value.
    floating_profit = None
    floating_basis = None
    if total_profit is not None and grid_profit is not None and grid_profit_source == "BINANCE_EXACT":
        floating_profit = total_profit - grid_profit
        floating_basis = "TOTAL_MINUS_EXACT_GRID_PROFIT"

    return (
        ratio, price_range, profit_grid, profit_grid_source, ratio_source,
        ratio_estimate_low, ratio_estimate_mid, ratio_estimate_high, ratio_status,
        grid_profit, total_profit, grid_profit_source, estimate_low, estimate_mid,
        estimate_high, total_source, floating_profit, floating_basis,
        estimate_method, estimate_confidence, estimate_grid_mode, estimate_grid_count, estimate_qty, estimate_qty_source
    )

def pages(page, endpoint, base_query, category, streamer, diagnostics, stats=None):
    """Lit toutes les pages d'une catégorie.

    Corrections par rapport à l'ancienne version :
      - nouvelles tentatives (attente exponentielle) au lieu d'abandonner à la 1re erreur ;
      - le statut HTTP est vérifié ;
      - le total annoncé par Binance est comparé au nombre de lignes collectées ;
      - le résultat (collecté / attendu / complet) est enregistré dans `stats`.
    """
    rows = []
    expected = None
    failed = False

    for page_no in range(1, MAX_PAGES + 1):
        query = dict(base_query)
        query.update(page=page_no, rows=PAGE_SIZE)

        data = None
        response = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                http = page.request.post(endpoint, data=query, timeout=30000)
                if not http.ok:
                    raise RuntimeError(f"HTTP {http.status}")
                response = http.json()
                if not isinstance(response, dict):
                    raise RuntimeError("JSON payload is not an object")
                raw_data = response.get("data")
                if not isinstance(raw_data, list):
                    raise RuntimeError("JSON payload has no list-valued data field")
                data = raw_data
                break
            except Exception as exc:
                diagnostics.append({
                    "endpoint": endpoint,
                    "category": category,
                    "page": page_no,
                    "attempt": attempt + 1,
                    "error": str(exc),
                })
                data = None
                if attempt < MAX_ATTEMPTS - 1:
                    page.wait_for_timeout(1000 * (2 ** attempt))

        if data is None:
            failed = True       # échec définitif de cette page : la catégorie est incomplète
            break

        if expected is None:
            total_raw = response.get("total")
            parsed_total = num(total_raw, None)
            # Missing total is not equivalent to an announced total of zero.
            expected = int(parsed_total) if parsed_total is not None and parsed_total >= 0 else None

        for item in data:
            item = dict(item)
            item["_category"] = category
            item["_streamer"] = streamer
            item["_lev"] = leverage(item)
            if str(item.get("strategyId") or "").strip() == "3232564":
                print("DEBUG_STRATEGY_3232564=" + json.dumps(item, ensure_ascii=False, sort_keys=True))
            rows.append(item)

        if len(data) < PAGE_SIZE or page_no * PAGE_SIZE >= (expected or 0):
            break
    else:
        failed = True           # plafond MAX_PAGES atteint : on n'a peut-être pas tout lu

    if stats is not None:
        strategy_ids = [
            str(item.get("strategyId") or "").strip()
            for item in rows
            if str(item.get("strategyId") or "").strip()
        ]
        unique_collected = len(set(strategy_ids))
        stats[category] = {
            "collected": len(rows),
            "unique_collected": unique_collected,
            "duplicate_rows": len(rows) - unique_collected,
            "expected": expected,
            # We only mark a category complete when Binance gave us a total
            # and the unique IDs cover that total. Unknown totals fail closed.
            "complete": (not failed) and expected is not None and unique_collected >= expected,
        }
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
            "gridMode": _grid_mode(x.get("strategyParams") or {}) or estimate_grid_mode,
            "gridCount": (
                first_num(x.get("strategyParams") or {}, ["gridCount", "gridNum", "numberOfGrids", "gridNumber"])
                if first_num(x.get("strategyParams") or {}, ["gridCount", "gridNum", "numberOfGrids", "gridNumber"]) is not None
                else ("" if estimate_grid_count is None else estimate_grid_count)
            ),
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
            url = response.url
            low_url = url.lower()
            # Keep a compact record of relevant non-JSON responses too. Some
            # detail-page data may be delivered through HTML/other transports;
            # previously these were silently discarded before URL inspection.
            if "json" not in content_type:
                if active_sid["value"] and any(token in low_url for token in
                        ("strategy", "grid", "detail", "profit", "position", "order")):
                    debug_write("DETAIL_NONJSON_RESPONSE sid=" + str(active_sid["value"]) +
                                " status=" + str(response.status) +
                                " contentType=" + content_type +
                                " url=" + url[:1800])
                return
            body = response.text()
            lowered = body.lower()
            # Capture candidate capital fields only from data endpoints.
            # Localization JSON contains labels such as "Total Investment", not
            # actual monetary values, and must never count as capital evidence.
            if active_sid["value"] and "/api/i18n/" not in url.lower():
                try:
                    payload_for_capital = json.loads(body)
                    candidate_fields = collect_capital_fields(payload_for_capital)
                    if candidate_fields:
                        CAPITAL_FIELD_DIAGNOSTICS.append({
                            "strategyId": str(active_sid["value"]),
                            "url": url,
                            "httpStatus": response.status,
                            "fields": candidate_fields,
                        })
                        debug_write("CAPITAL_FIELDS sid=" + str(active_sid["value"]) +
                                    " url=" + url + " fields=" +
                                    json.dumps(candidate_fields, ensure_ascii=False)[:20000])
                except Exception:
                    pass

            # Record the public Marketplace API response schema, not its full
            # payload. This reveals which fields Binance actually publishes
            # (including PNL/ROI chart fields) without confusing translations
            # or analytics calls with bot data.
            lower_url = url.lower()
            if ("/bapi/futures/v1/public/future/common/strategy/landing-page/" in lower_url
                    or "/bapi/futures/v1/public/future/common/grid/" in lower_url):
                try:
                    payload_schema = json.loads(body)
                    schema_key = (url.split("?")[0], response.status)
                    if schema_key not in PUBLIC_BAPI_ENDPOINT_SEEN:
                        PUBLIC_BAPI_ENDPOINT_SEEN.add(schema_key)
                        data_value = payload_schema.get("data") if isinstance(payload_schema, dict) else None
                        sample_record = data_value[0] if isinstance(data_value, list) and data_value else data_value
                        PUBLIC_BAPI_ENDPOINT_SCHEMAS.append({
                            "url": url.split("?")[0],
                            "httpStatus": response.status,
                            "rootKeys": list(payload_schema.keys()) if isinstance(payload_schema, dict) else [],
                            "keyPaths": summarize_json_schema(payload_schema),
                            "dataSampleKeys": list(sample_record.keys()) if isinstance(sample_record, dict) else [],
                            "capitalCandidateFields": collect_capital_fields(payload_schema),
                        })
                        debug_write("PUBLIC_BAPI_SCHEMA url=" + url.split("?")[0] +
                                    " status=" + str(response.status) +
                                    " rootKeys=" + json.dumps(list(payload_schema.keys()) if isinstance(payload_schema, dict) else []) +
                                    " dataSampleKeys=" + json.dumps(list(sample_record.keys()) if isinstance(sample_record, dict) else []) +
                                    " keyPaths=" + json.dumps(summarize_json_schema(payload_schema)[:60]))
                except Exception as exc:
                    debug_write("PUBLIC_BAPI_SCHEMA_ERROR url=" + url + " " + str(exc))
            if "/bapi/" in url.lower():
                # Inventory every Binance BAPI response during detail-page
                # navigation, including endpoints whose paths do not contain
                # "strategy", "grid", "profit", "position" or "order". The
                # previous filter could silently miss the actual detail API.
                debug_write("DETAIL_BAPI_RESPONSE sid=" + str(active_sid["value"]) +
                            " status=" + str(response.status) +
                            " contentType=" + content_type +
                            " url=" + url[:1800] +
                            " bytes=" + str(len(body)))
                try:
                    payload_schema = json.loads(body)
                    schema_key = (url.split("?")[0], response.status)
                    # Preserve the small, actionable error envelope from BAPI
                    # responses. This helps distinguish login-gated detail data,
                    # endpoint failures and genuine empty metric payloads without
                    # storing whole response bodies.
                    if isinstance(payload_schema, dict):
                        api_success = payload_schema.get("success")
                        api_code = payload_schema.get("code")
                        api_message = payload_schema.get("message", payload_schema.get("msg", ""))
                        api_detail = payload_schema.get("messageDetail", "")
                        if response.status >= 400 or api_success is False or (api_code not in (None, 0, "0", "000000")):
                            debug_write("DETAIL_BAPI_ERROR sid=" + str(active_sid["value"]) +
                                        " status=" + str(response.status) +
                                        " url=" + url.split("?")[0] +
                                        " code=" + str(api_code)[:200] +
                                        " success=" + str(api_success) +
                                        " message=" + str(api_message)[:500] +
                                        " detail=" + str(api_detail)[:500])
                    if schema_key not in PUBLIC_BAPI_ENDPOINT_SEEN:
                        PUBLIC_BAPI_ENDPOINT_SEEN.add(schema_key)
                        data_value = payload_schema.get("data") if isinstance(payload_schema, dict) else None
                        sample_record = data_value[0] if isinstance(data_value, list) and data_value else data_value
                        debug_write("DETAIL_BAPI_SCHEMA sid=" + str(active_sid["value"]) +
                                    " url=" + url.split("?")[0] +
                                    " status=" + str(response.status) +
                                    " rootKeys=" + json.dumps(list(payload_schema.keys()) if isinstance(payload_schema, dict) else []) +
                                    " dataSampleKeys=" + json.dumps(list(sample_record.keys()) if isinstance(sample_record, dict) else []) +
                                    " keyPaths=" + json.dumps(summarize_json_schema(payload_schema)[:100]))
                except Exception as exc:
                    debug_write("DETAIL_BAPI_SCHEMA_ERROR url=" + url[:1800] + " " + str(exc))
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
            if "/bapi/" in low:
                debug_write("DETAIL_BAPI_REQUEST sid=" + str(active_sid["value"]) +
                            " url=" + url[:1800] + " method=" + str(request.method) +
                            " post=" + str(request.post_data or "")[:5000])
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
            # Correction : le champ peut s'appeler matchedTrades, matchedCount ou
            # latestMatchedCount selon la réponse ; l'ancien code ne lisait que les deux derniers.
            matched_values = [
                num(row.get(key), 0.0)
                for key in ("matchedTrades", "matchedCount", "latestMatchedCount", "totalMatchedTrades")
            ]
            matched = max(matched_values, default=0.0)
            pnl = num(row.get("pnl"), 0.0)
            if running <= 0 or matched <= 0 or pnl == 0:
                continue

            seen.add(sid)
            candidates.append((path, symbol, sid, row.get("_category"), running, matched, pnl))

        # Deterministic diagnostic sample: deliberately probe the OLDEST public
        # strategy IDs first, because the newest Marketplace IDs often resolve
        # to "Pending Trigger" placeholders. Also keep a separate recent sample
        # to detect if Binance changed the detail-page behavior for new bots.
        # Sorting descending here would accidentally select only the newest IDs
        # and defeat the historical-endpoint investigation.
        candidates.sort(key=lambda item: (int(item[2]) if str(item[2]).isdigit() else 10**18, item[2]))
        spot_all = [x for x in candidates if "spot grid" in str(x[3]).lower()]
        futures_all = [x for x in candidates if "futures grid" in str(x[3]).lower()]
        spot_old = spot_all[:8]
        futures_old = futures_all[:8]
        spot_recent = sorted(spot_all, key=lambda item: int(item[2]) if str(item[2]).isdigit() else -1, reverse=True)[:2]
        futures_recent = sorted(futures_all, key=lambda item: int(item[2]) if str(item[2]).isdigit() else -1, reverse=True)[:2]
        spot = spot_old + [x for x in spot_recent if x[2] not in {y[2] for y in spot_old}]
        futures = futures_old + [x for x in futures_recent if x[2] not in {y[2] for y in futures_old}]
        candidates = spot + futures

        known_probe_ids = [
            ("TRXETH", "6850680", "Spot Grid"),
            ("XRPBTC", "3232564", "Spot Grid"),
            ("TSTUSDT", "9161957", "Spot Grid"),
        ]
        existing = {str(x[2]) for x in candidates}
        for symbol, sid, category in known_probe_ids:
            if sid not in existing:
                candidates.append(("https://www.binance.com/en/trading-bots/spot/grid/detail"
                                   if "spot grid" in category.lower()
                                   else "https://www.binance.com/en/trading-bots/futures/grid/detail",
                                   symbol, sid, category, 0, 0, 0))

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
                    if any(token in lowered_resource for token in ("strategy", "grid", "detail", "profit", "position", "order")):
                        debug_write("DETAIL_RESOURCE sid=" + str(sid) + " url=" + str(resource_url)[:1800])
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
                    # Detect placeholder pages BEFORE recording capital values.
                    # The previous order accidentally logged "Total Investment: 0"
                    # from Pending Trigger pages as if it were an observed metric.
                    pending_detail = ("Pending Trigger" in body_text or "Duration --" in body_text)
                    page_state = {
                        "pendingTrigger": "Pending Trigger" in body_text,
                        "durationPlaceholder": "Duration --" in body_text,
                        "hasGridDetails": "Grid Details" in body_text,
                        "hasOrderHistory": "Order History" in body_text,
                        "hasLoginPrompt": ("Log In" in body_text and "Sign Up" in body_text),
                        "hasPendingOrder": "Pending Order" in body_text,
                    }
                    try:
                        page_title = page.title()
                    except Exception:
                        page_title = ""
                    debug_write("DETAIL_PAGE_STATE sid=" + str(sid) +
                                " pending=" + str(pending_detail) +
                                " state=" + json.dumps(page_state, ensure_ascii=False) +
                                " title=" + str(page_title)[:300] +
                                " bodyStart=" + body_text[:900].replace("\\n", " | "))
                    visible_capital = []
                    for capital_label in ("Total Investment", "Initial Investment", "Investment Amount", "Capital Invested"):
                        capital_value = parse_visible_metric(body_text, capital_label)
                        if capital_value is not None:
                            visible_capital.append({"label": capital_label, "value": capital_value})
                    if visible_capital and not pending_detail:
                        CAPITAL_FIELD_DIAGNOSTICS.append({
                            "strategyId": str(sid),
                            "url": url,
                            "source": "BINANCE_DETAIL_VISIBLE_TEXT",
                            "fields": visible_capital,
                        })
                        debug_write("CAPITAL_VISIBLE sid=" + str(sid) +
                                    " url=" + url + " fields=" +
                                    json.dumps(visible_capital, ensure_ascii=False))
                    elif visible_capital and pending_detail:
                        debug_write("CAPITAL_VISIBLE_REJECTED_PENDING sid=" + str(sid) +
                                    " url=" + url + " fields=" +
                                    json.dumps(visible_capital, ensure_ascii=False))
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
    stats = {}      # décompte collecté / attendu par catégorie
    now = dt.datetime.now(dt.timezone.utc)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        rows = pages(
            page, TOP,
            {"strategyType": 1, "symbol": "", "zone": "", "sort": "pnl"},
            "Spot Grid", "SPOT_GRID", diagnostics, stats,
        )

        # Binance currently exposes multiple bot families through this
        # landing-page endpoint. Keep only strategies that actually appear
        # in the active public marketplace and pass the <=1x filter below.
        for strategy_type in range(2, 21):
            rows.extend(
                pages(
                    page, TOP,
                    {"strategyType": strategy_type, "symbol": "", "zone": "", "sort": "pnl"},
                    f"Marketplace type {strategy_type}", f"TYPE_{strategy_type}", diagnostics, stats,
                )
            )

        debug_root = Path(__file__).resolve().parent.parent / "strategy_detail_debug.log"
        debug_root.write_text("MAIN_REACHED_DETAIL_CALL\\n", encoding="utf-8")
        print("MAIN_REACHED_DETAIL_CALL " + str(debug_root))
        captured_detail_metrics = capture_marketplace_detail_calls(page, rows)

        # Save a dedicated report so we can verify whether Binance's public
        # detail responses expose the actual capital reference. Keep raw values,
        # JSON paths, endpoint URLs, and strategy IDs for manual verification.
        # Marketplace minInvestment is also recorded separately: it is not
        # assumed to equal the bot's actual invested capital.
        marketplace_capital_fields = []
        for row in rows:
            sid = str(row.get("strategyId") or "").strip()
            if sid not in {"6850680", "3232564", "9161957"}:
                continue
            raw_fields = collect_capital_fields(row)
            if raw_fields or row.get("minInvestment") is not None:
                marketplace_capital_fields.append({
                    "strategyId": sid,
                    "symbol": row.get("symbol"),
                    "category": row.get("_category"),
                    "source": "BINANCE_MARKETPLACE_LISTING_PAYLOAD",
                    "fields": raw_fields,
                    "minInvestment": row.get("minInvestment", row.get("minimumInvestment")),
                    "roi": row.get("roi"),
                    "pnl": row.get("pnl"),
                })
        with (OUT / "capital_field_diagnostics.json").open("w", encoding="utf-8") as handle:
            json.dump({
                "generated_utc": now.isoformat(),
                "purpose": "Find public Binance fields that may represent actual bot investment/capital; diagnostic only, not a validated capital metric.",
                "sample_strategy_ids": ["6850680", "3232564", "9161957"],
                "field_name_candidates": sorted(CAPITAL_FIELD_NAMES),
                "marketplace_listing_fields": marketplace_capital_fields,
                "detail_response_fields": CAPITAL_FIELD_DIAGNOSTICS,
                "public_bapi_endpoint_schemas": PUBLIC_BAPI_ENDPOINT_SCHEMAS,
                "interpretation_rule": "A field name alone does not prove its accounting basis or units. Do not treat minInvestment, inferred PNL/ROI capital, initial capital, current capital, or margin as interchangeable without endpoint-level verification.",
            }, handle, ensure_ascii=False, indent=2)

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
                "Futures DCA", "UM_DCA", diagnostics, stats,
            )
        )

        symbols = [str(x.get("symbol") or "").strip() for x in rows]
        prices = fetch_prices(page, symbols)
        browser.close()

    current = build_current(rows, prices)
    write_current(current)
    write_summary(current)
    snapshot, removed = write_history(current, now)

    incomplete = sorted(name for name, s in stats.items() if not s["complete"])
    collection_complete = not incomplete
    if not collection_complete:
        # Annotation visible dans l'onglet Actions de GitHub
        print("::warning::Collecte incomplète pour : " + ", ".join(incomplete))

    with (OUT / "scan_meta.json").open("w", encoding="utf-8") as handle:
        json.dump({
            "scan_utc": now.isoformat(),
            "raw_rows": len(rows),
            "tradable_leverage_le_1": len(current),
            "collection_complete": collection_complete,
            "incomplete_categories": incomplete,
            "collection": stats,
            "collection_method": {
                "page_size": PAGE_SIZE,
                "max_pages_per_category": MAX_PAGES,
                "max_attempts_per_page": MAX_ATTEMPTS,
                "sort": "pnl",
                "strategy_types_scanned": [1, *range(2, 21)],
                "completeness_rule": "Each category must expose a total and the unique collected strategy IDs must cover that total; exhausted retries or page cap means incomplete.",
                "limitations": [
                    "Marketplace sorting by PNL can change during pagination; total/unique checks detect many, but not every, skip/duplicate caused by live resorting.",
                    "Only strategy types 1 through 20 are queried; types above 20 are not covered unless Binance documents/discovers them.",
                    "The endpoint total can change during a live scan, so completeness is relative to the first successful page response."
                ],
            },
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
            "collection": stats,
            "filter": "active public Binance Bot Marketplace strategies with leverage <= 1",
        }, handle, ensure_ascii=False, indent=2)

    print(f"DONE raw={len(rows)} current={len(current)} complete={collection_complete} snapshot={snapshot} removed={removed}")


if __name__ == "__main__":
    main()
