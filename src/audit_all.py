#!/usr/bin/env python3
"""src/audit_all.py -- collecte des bots publics Binance Spot Grid uniquement.

Étape 1 du pipeline (audit_all -> validate raw -> build_multicriteria -> validate final).
Écrit le CSV brut results/current_multicriteria.csv (avec colonnes de provenance),
l'archive horaire results/history/ et results/scan_meta.json.

Grid Profit : valeur officielle lue sur la page de détail Binance (JSON ou texte visible) ;
à défaut, estimation bornée (Low/Mid/High) reconstruite depuis la géométrie de la grille.
Profit par grille après frais : valeur Binance si exposée, sinon formule documentée (FEE).
"""
import csv
import datetime as dt
import gzip
import json
import re
import time
import zlib
from pathlib import Path
from urllib.parse import quote

OUT = Path("results")
HISTORY = OUT / "history"

BASE = "https://www.binance.com/bapi/futures/v1/public/future/common/strategy/landing-page/"
TOP = BASE + "queryTopStrategy"
DCA = BASE + "queryTopUmDcaStrategy"
DETAIL_PAGES = {
    "spot grid": "https://www.binance.com/en/trading-bots/spot/grid/detail",
    "futures grid": "https://www.binance.com/en/trading-bots/futures/grid/detail",
}

FIELDS = [
    "strategyId", "category", "strategyType", "symbol", "leverage",
    "minInvestment", "runningTime", "roi", "pnl", "matchedTrades", "mdd7d",
    "gridProfit", "gridProfitSource", "gridMode", "gridCount", "qtyPerOrderEstimate", "qtyPerOrderSource",
    "gridProfitEstimateLow", "gridProfitEstimateMid", "gridProfitEstimateHigh",
    "totalProfit", "totalProfitSource", "gridProfitTotalProfitRatio", "gridProfitTotalProfitRatioSource",
    "gridProfitTotalProfitRatioEstimateLow", "gridProfitTotalProfitRatioEstimateMid",
    "gridProfitTotalProfitRatioEstimateHigh", "gridProfitTotalProfitRatioStatus",
    "floatingProfit", "gridProfitEstimateMethod", "gridProfitConfidence", "gridProfitEstimateStatus",
    "floatingProfitSource", "currentPrice", "priceRange", "profitPerGridAfterFees",
]

FEE = 0.001                     # frais de référence par ordre (Spot 0,1 %) : source unique
RETENTION_DAYS = 30
PAGE_SIZE, MAX_PAGES, MAX_ATTEMPTS = 100, 400, 3
# Probe broadly to discover Marketplace strategyType values instead of assuming 1..20.
MAX_STRATEGY_TYPE = 100
RUN_BUDGET_S = 1500             # le job GitHub est limité à 30 min
DETAIL_BUDGET_S = 900           # temps max consacré aux pages de détail (Grid Profit exact)
DETAIL_GIVEUP_AFTER = 25       # abandon des pages de détail si aucune métrique lue après N visites
USD_STABLE = {"USDT", "USDC", "FDUSD", "TUSD", "USDP", "BUSD", "DAI", "USD1", "U"}
# USD1 et le suffixe U apparaissent dans les symboles du Marketplace public Binance.
# JPY est une devise de cotation ; sa conversion USD est reconstruite par cross-rate BTC/ETH.
QUOTES = tuple(sorted(USD_STABLE | {"BTC", "ETH", "BNB", "XRP", "EUR", "TRY", "BRL", "JPY"}, key=len, reverse=True))

LOWER = ["lowerLimit", "lowerPrice", "gridLowerLimit", "gridLowerPrice", "minPrice", "lower"]
UPPER = ["upperLimit", "upperPrice", "gridUpperLimit", "gridUpperPrice", "maxPrice", "upper"]
GRIDS = ["gridCount", "gridNum", "numberOfGrids", "gridNumber"]
FLOAT_KEYS = ["floatingPnl", "unrealizedPnl", "floatProfit", "floatingProfit"]
DETAIL_ENDPOINT_TOKENS = ("/strategy/landing-page/queryroichart", "/strategy/detail", "/strategy/info",
                          "/grid/detail", "/grid/query", "/grid/strategy", "/strategy/query")

EXACT_RATIO_SOURCES = ("BINANCE_DETAIL_GRID_DIV_TOTAL_EXACT", "BINANCE_DETAIL_TOTAL_MINUS_FLOATING_DIV_TOTAL_EXACT")
ESTIMATED_RATIO_SOURCE = "RECONSTRUCTED_GRID_PROFIT_DIV_BINANCE_MARKETPLACE_TOTAL_PROFIT"


# --------------------------------------------------------------------------- utilitaires
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


def blank(value):
    return "" if value is None else value


def normalize_symbol(symbol):
    return str(symbol or "").strip().upper().replace("/", "").replace("-", "").replace("_", "")


def fmt_price(value):
    if value is None:
        return ""
    value = float(value)
    if value == 0:
        return "0"
    return (f"{value:.8f}" if abs(value) >= 1 else f"{value:.12f}").rstrip("0").rstrip(".")


def _norm_key(key):
    return str(key).replace("_", "").replace("-", "").lower()


def find_exact_numeric_key(obj, keys, lists=True):
    """Première valeur numérique d'une clé exacte. lists=False ignore les listes
    (séries de graphiques : le premier point serait une valeur ancienne, pas la valeur courante)."""
    wanted = {_norm_key(k) for k in keys}
    if isinstance(obj, dict):
        for key, value in obj.items():
            if _norm_key(key) in wanted:
                parsed = num(value, None)
                if parsed is not None:
                    return parsed
        for value in obj.values():
            found = find_exact_numeric_key(value, keys, lists)
            if found is not None:
                return found
    elif isinstance(obj, list) and lists:
        for value in obj:
            found = find_exact_numeric_key(value, keys, lists)
            if found is not None:
                return found
    return None


def first_num(obj, paths):
    for path in paths:
        value = obj
        for key in path.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        try:
            if value not in (None, "", "null"):
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def leverage(x):
    value = (x.get("strategyParams") or {}).get("leverage", x.get("leverage"))
    if value in (None, "", "null"):
        # Spot Grid n'a pas de levier ; levier inconnu pour les autres familles -> exclu.
        return 1.0 if "spot grid" in str(x.get("_category", "")).lower() else 99.0
    try:
        return float(value)
    except Exception:
        return 99.0


def _quote_asset(symbol, symbol_assets=None):
    """Devise de cotation tirée des métadonnées Binance, sinon repli historique."""
    normalized = normalize_symbol(symbol)
    if symbol_assets:
        market = symbol_assets.get(normalized)
        if isinstance(market, dict) and market.get("quoteAsset"):
            return str(market["quoteAsset"]).upper()
        if isinstance(market, str) and market:
            return market.upper()
    return next((q for q in QUOTES if normalized.endswith(q) and len(normalized) > len(q)), "")


def fetch_exchange_info(page):
    """Découvre dynamiquement les marchés Spot actifs et leurs actifs base/quote."""
    endpoints = (
        "https://data-api.binance.vision/api/v3/exchangeInfo",
        "https://api.binance.com/api/v3/exchangeInfo",
        "https://api-gcp.binance.com/api/v3/exchangeInfo",
    )
    for endpoint in endpoints:
        try:
            response = page.request.get(endpoint, timeout=30000)
            if not response.ok:
                continue
            payload = response.json()
            symbols = payload.get("symbols", []) if isinstance(payload, dict) else []
            found = {}
            for market in symbols:
                if not isinstance(market, dict):
                    continue
                if market.get("status") not in (None, "TRADING"):
                    continue
                if market.get("isSpotTradingAllowed") is False:
                    continue
                symbol = normalize_symbol(market.get("symbol"))
                base, quote_asset = market.get("baseAsset"), market.get("quoteAsset")
                if symbol and base and quote_asset:
                    found[symbol] = {"baseAsset": str(base).upper(), "quoteAsset": str(quote_asset).upper()}
            if found:
                return found
        except Exception:
            continue
    return {}


def _quote_usd_price(symbol, prices, symbol_assets=None):
    """USD par unité de devise de cotation. Préfère les taux directs puis les cross-rates Binance."""
    quote_asset = _quote_asset(symbol, symbol_assets)
    if not quote_asset:
        return None
    if quote_asset in USD_STABLE:
        return 1.0
    direct = prices.get(quote_asset + "USDT")
    if direct is not None and direct > 0:
        return direct
    inverse = prices.get("USDT" + quote_asset)
    if inverse is not None and inverse > 0:
        return 1.0 / inverse
    if symbol_assets:
        candidates = sorted(
            (market for market, assets in symbol_assets.items()
             if assets.get("quoteAsset") == quote_asset and assets.get("baseAsset")),
            key=lambda market: (0 if market.startswith(("BTC", "ETH", "BNB", "XRP", "SOL")) else 1, market),
        )
        for market in candidates:
            base = symbol_assets[market]["baseAsset"]
            quote_price = prices.get(market)
            if quote_price is None or quote_price <= 0:
                continue
            base_usd = prices.get(base + "USDT")
            if base_usd is not None and base_usd > 0:
                return base_usd / quote_price
            base_inverse = prices.get("USDT" + base)
            if base_inverse is not None and base_inverse > 0:
                return 1.0 / (base_inverse * quote_price)
    else:
        # Repli hors exchangeInfo : quelques marchés de référence déjà demandés
        # permettent de préserver les conversions lors d'une panne des métadonnées.
        for base in ("BTC", "ETH", "BNB", "XRP", "SOL"):
            quote_price = prices.get(base + quote_asset)
            base_usd = prices.get(base + "USDT")
            if quote_price is not None and quote_price > 0 and base_usd is not None and base_usd > 0:
                return base_usd / quote_price
            base_inverse = prices.get("USDT" + base)
            if quote_price is not None and quote_price > 0 and base_inverse is not None and base_inverse > 0:
                return 1.0 / (base_inverse * quote_price)
    return None


def _grid_mode(params):
    mode = str(params.get("type", params.get("gridType", params.get("gridMode", "")))).strip().upper()
    if mode in {"2", "GEO", "GEOMETRIC"}:
        return "GEOMETRIC"
    if mode in {"1", "ARITH", "ARITHMETIC"}:
        return "ARITHMETIC"
    return ""


def _grid_levels(lower, upper, grids, mode):
    if mode == "GEOMETRIC":
        ratio = (upper / lower) ** (1.0 / grids)
        return [lower * ratio ** i for i in range(grids + 1)]
    step = (upper - lower) / grids
    return [lower + step * i for i in range(grids + 1)]


def _roi_interval(raw_roi):
    """(bas, milieu, haut) du ROI affiché, arrondi par Binance."""
    try:
        raw = str(raw_roi).strip().replace(",", "")
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value == 0:
        # Un ROI affiché à 0 est arrondi : il ne permet pas d’inverser PNL/ROI
        # de façon finie et fiable. estimate_grid_profit essaiera le capital minimum.
        return None
    step = 10 ** (-len(raw.split(".", 1)[1])) if "." in raw else 1.0
    low, high = value - step / 2, value + step / 2
    # L'intervalle ne doit pas franchir 0 (ROI négatif accepté : PNL et ROI de même signe).
    return (max(low, 1e-12), value, high) if value > 0 else (low, value, min(high, -1e-12))


# --------------------------------------------------------------------------- profit par grille
def profit_per_grid(item, lower, upper, grids, mode):
    """Profit net d'un cycle achat/vente de grille, après frais (formules Binance, c = FEE).
    Géométrique : valeur unique. Arithmétique : fourchette (le % dépend du niveau de prix)."""
    direct = first_num(item, ["profitPerGrid", "profitGrid", "strategyStats.profitPerGrid",
                              "strategyStats.profitGrid", "stats.profitPerGrid", "stats.profitGrid"])
    if direct is not None:
        return f"{direct:.4f}%"
    if not (lower and upper and grids and grids > 0 and lower > 0 and upper > lower and mode):
        return ""
    try:
        if mode == "GEOMETRIC":
            ratio = (upper / lower) ** (1.0 / grids)
            return f"{((1 - FEE) * ratio - 1 - FEE) * 100:.4f}%"
        d = (upper - lower) / grids
        maximum = ((1 - FEE) * d / lower - 2 * FEE) * 100
        minimum = ((upper * (1 - FEE)) / (upper - d) - 1 - FEE) * 100
        lo, hi = sorted((minimum, maximum))
        return f"{lo:.4f}% - {hi:.4f}%"
    except (ZeroDivisionError, ValueError, OverflowError):
        return ""


# --------------------------------------------------------------------------- estimation du Grid Profit
def estimate_grid_profit(item, total_profit_usd, prices, symbol_assets=None):
    """Estimation bornée du Grid Profit (Spot Grid) ; None si les données ne suffisent pas."""
    if str(item.get("_category") or "").lower() != "spot grid":
        return None
    params = item.get("strategyParams") or {}

    def pnum(paths):
        value = first_num(params, paths)
        return value if value is not None else first_num(item, paths)

    lower, upper, grids = pnum(LOWER), pnum(UPPER), pnum(GRIDS)
    current = pnum(["currentPrice", "lastPrice", "marketPrice", "price", "_audit_current_price"])
    matched = num(item.get("matchedTrades", item.get("matchedCount", item.get("totalMatchedTrades", 0))), 0.0)
    roi_raw = item.get("roi", item.get("roiPct", item.get("roiRate", 0)))
    roi_interval = _roi_interval(roi_raw)
    min_investment = num(item.get("minInvestment", item.get("minimumInvestment")), None)
    # ROI affiché à zéro : utiliser le capital minimum Binance comme proxy explicite,
    # plutôt que d’abandonner l’estimation ou de diviser par zéro. Ce proxy reste LOW confidence.
    use_min_investment = roi_interval is None and min_investment is not None and min_investment > 0
    if (lower is None or upper is None or grids is None or current is None or grids < 2 or lower <= 0
            or upper <= lower or matched <= 0 or total_profit_usd in (None, 0)
            or (roi_interval is None and not use_min_investment)):
        return None
    try:
        grids_i, mode = int(round(grids)), _grid_mode(params)
        if grids_i < 2 or not mode:
            return None
        if use_min_investment:
            # CapitalMinimum est un proxy, pas une déclaration du capital réellement investi.
            inv_low = inv_mid = inv_high = float(min_investment)
        else:
            roi_low, roi_mid, roi_high = roi_interval
            invs = [float(total_profit_usd) / (r / 100.0) for r in (roi_low, roi_mid, roi_high)]
            if min(invs) <= 0:
                return None                  # PNL et ROI de signes opposés : base incohérente
            inv_mid, inv_low, inv_high = invs[1], min(invs), max(invs)
        quote_usd = _quote_usd_price(str(item.get("symbol") or ""), prices, symbol_assets)
        if not quote_usd or quote_usd <= 0:
            return None
        levels = _grid_levels(lower, upper, grids_i, mode)
        inside = any(p < current for p in levels) and any(p > current for p in levels)

        def allocation(start):      # capital par unité de quantité, avec l'état de la grille au prix `start`
            return (sum(p for p in levels if p < start) + sum(1 for p in levels if p > start) * start) * (1 + FEE)

        # Prix dans la plage : état courant connu. Hors plage : le prix de départ du bot est inconnu ;
        # on borne l'allocation sur toute la plage (départ à la borne basse, au centre, à la borne haute).
        starts = [current] if inside else [lower, (lower + upper) / 2, upper]
        allocs = [allocation(st) for st in starts]
        if min(allocs) <= 0:
            return None
        qtys = [(inv / quote_usd) / a for inv in (inv_low, inv_mid, inv_high) for a in allocs]
        qty_mid = (inv_mid / quote_usd) / (allocs[0] if inside else allocs[1])
        # Profits nets négatifs conservés ; bornes sur toutes les combinaisons extrêmes.
        net = [(levels[i + 1] - levels[i]) - FEE * (levels[i] + levels[i + 1]) for i in range(grids_i)]
        possible = [matched * n * q for n in net for q in qtys]
        raw_roi = str(roi_raw or "").strip()
        precision = len(raw_roi.split(".", 1)[1]) if "." in raw_roi else 0
        low_conf = use_min_investment or abs(roi_mid) < 0.1 or precision <= 1 or not inside
        return {
            "low": min(possible) * quote_usd,
            "mid": matched * qty_mid * (sum(net) / len(net)) * quote_usd,
            "high": max(possible) * quote_usd,
            "method": ("BINANCE_FORMULA_RECONSTRUCTION_MIN_INVESTMENT_FALLBACK" if use_min_investment
                       else "BINANCE_FORMULA_RECONSTRUCTION_ROUNDED_ROI_AND_GRID_RANGE")
                      + ("" if inside else "_PRICE_OUT_OF_RANGE_START_BOUNDED"),
            "confidence": "LOW" if low_conf else "MEDIUM",
            "gridMode": mode, "gridCount": grids_i, "qty": qty_mid,
            "qtySource": "RECONSTRUCTED_FROM_ROUNDED_ROI_AND_GRID_STATE" if inside
                         else "RECONSTRUCTED_FROM_ROUNDED_ROI_ASSUMED_MID_RANGE_START",
        }
    except (ZeroDivisionError, ValueError, OverflowError):
        return None


def _detail_matches_marketplace(symbol, detail_total, marketplace_pnl):
    """Les valeurs de la page de détail sont dans la devise de cotation. On ne les accepte comme
    USD que pour une cotation stable USD ET si elles sont cohérentes avec le PNL Marketplace
    (garde-fou contre une unité différente ; tolérance large car la page est en direct)."""
    if _quote_asset(symbol) not in USD_STABLE:
        return False
    return abs(detail_total - marketplace_pnl) <= max(1.0, 0.5 * abs(marketplace_pnl))


def grid_metrics(item, prices, symbol_assets=None):
    """Toutes les métriques Grid Profit / ratio / profit par grille d'un bot, avec provenance."""
    params = item.get("strategyParams") or {}
    lower, upper, grids = first_num(params, LOWER), first_num(params, UPPER), first_num(params, GRIDS)
    mode = _grid_mode(params)
    category = str(item.get("_category") or "").lower()
    marketplace_pnl = num(item.get("pnl"), None)
    detail_grid = num(item.get("_detail_grid_profit"), None)
    detail_total = num(item.get("_detail_total_profit"), None)
    detail_floating = num(item.get("_detail_floating_pnl"), None)

    # Total Profit : PNL Marketplace (USD) pour le Spot ; la valeur de détail Binance ne le remplace
    # que si sa base est prouvée identique. Hors Spot : valeur de détail uniquement.
    total_profit, total_source = None, ""
    if category == "spot grid" and marketplace_pnl is not None:
        total_profit, total_source = marketplace_pnl, "BINANCE_MARKETPLACE_PNL_USD_AS_TOTAL_PROFIT"
        if detail_total not in (None, 0) and _detail_matches_marketplace(
                item.get("symbol"), detail_total, marketplace_pnl):
            total_profit, total_source = detail_total, "BINANCE_DETAIL_TOTAL_PROFIT"
    elif detail_total is not None:
        total_profit, total_source = detail_total, "BINANCE_DETAIL_TOTAL_PROFIT"

    m = {
        "ratio": "", "ratioSource": "UNAVAILABLE", "ratioLow": None, "ratioMid": None, "ratioHigh": None,
        "ratioStatus": "UNAVAILABLE", "gridProfit": None, "gridSource": "", "low": None, "mid": None,
        "high": None, "totalProfit": total_profit, "totalSource": total_source, "floating": None,
        "floatingBasis": "", "method": "", "confidence": "", "gridMode": mode,
        "gridCount": grids, "qty": None, "qtySource": "",
        "priceRange": f"{fmt_price(lower)} - {fmt_price(upper)}" if lower is not None and upper is not None else "",
        "profitPerGrid": profit_per_grid(item, lower, upper, grids, mode),
    }

    # Ratio exact : numérateur et dénominateur sur la même base Binance.
    if total_source == "BINANCE_DETAIL_TOTAL_PROFIT" and detail_total not in (None, 0):
        exact_grid = None
        if detail_grid is not None:
            exact_grid, m["ratioSource"], m["method"] = detail_grid, EXACT_RATIO_SOURCES[0], "BINANCE_EXACT_DETAIL"
        elif detail_floating is not None:
            exact_grid, m["ratioSource"] = detail_total - detail_floating, EXACT_RATIO_SOURCES[1]
            m["method"] = "BINANCE_EXACT_DETAIL_RECONSTRUCTION"
        if exact_grid is not None:
            m.update(gridProfit=exact_grid, gridSource="BINANCE_EXACT", low=exact_grid, mid=exact_grid,
                     high=exact_grid, confidence="HIGH", ratioStatus="EXACT",
                     ratio=f"{exact_grid / detail_total:.6f}",
                     floating=detail_total - exact_grid, floatingBasis="TOTAL_MINUS_EXACT_GRID_PROFIT")

    # Sinon : estimation bornée, jamais présentée comme exacte.
    if m["gridProfit"] is None and total_profit not in (None, 0):
        est = estimate_grid_profit(item, total_profit, prices, symbol_assets)
        if est:
            m.update(gridProfit=est["mid"], gridSource="RECONSTRUCTED", low=est["low"], mid=est["mid"],
                     high=est["high"], method=est["method"], confidence=est["confidence"],
                     gridMode=est["gridMode"] or mode, gridCount=grids if grids is not None else est["gridCount"],
                     qty=est["qty"], qtySource=est["qtySource"], ratioSource=ESTIMATED_RATIO_SOURCE,
                     ratioStatus="ESTIMATED_NOT_EXACT",
                     ratioLow=min(est["low"] / total_profit, est["high"] / total_profit),   # un Total Profit négatif
                     ratioMid=est["mid"] / total_profit,                                      # inverse l'ordre
                     ratioHigh=max(est["low"] / total_profit, est["high"] / total_profit))
            m["ratio"] = f"{m['ratioMid']:.6f}"
    return m


# --------------------------------------------------------------------------- collecte
def extract_strategy_rows(payload):
    """Extrait une liste de stratégies depuis plusieurs variantes de réponse BAPI."""
    if not isinstance(payload, dict):
        return None, None
    total = num(payload.get("total", payload.get("totalCount", payload.get("count"))), None)
    data = payload.get("data")
    if isinstance(data, list):
        return data, total
    preferred = ("list", "rows", "items", "records", "strategies", "strategyList", "result", "listData")
    for root in (data, payload):
        if not isinstance(root, dict):
            continue
        for key in preferred:
            value = root.get(key)
            if isinstance(value, list) and (not value or any(
                    isinstance(v, dict) and any(k in v for k in ("strategyId", "algoId", "strategy_id"))
                    for v in value)):
                nested_total = num(root.get("total", root.get("totalCount", root.get("count"))), None)
                return value, nested_total if nested_total is not None else total
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            if not node or any(isinstance(v, dict) and any(
                    k in v for k in ("strategyId", "algoId", "strategy_id")) for v in node):
                return node, total
            stack.extend(node)
    return None, total


def pages(page, endpoint, base_query, category, streamer, diagnostics, stats=None):
    """Lit les pages, accepte plusieurs schémas BAPI et distingue erreur de schéma et page vide."""
    rows, expected, failed = [], None, False
    unsupported = False
    for page_no in range(1, MAX_PAGES + 1):
        query = dict(base_query, page=page_no, rows=PAGE_SIZE)
        data, response = None, None
        for attempt in range(MAX_ATTEMPTS):
            try:
                http = page.request.post(endpoint, data=query, timeout=30000)
                if not http.ok:
                    raise RuntimeError(f"HTTP {http.status}")
                response = http.json()
                data, response_total = extract_strategy_rows(response)
                if data is None:
                    keys = sorted(response.keys()) if isinstance(response, dict) else [type(response).__name__]
                    diagnostics.append({
                        "endpoint": endpoint, "category": category, "page": page_no,
                        "attempt": attempt + 1, "error": "Unrecognized strategy-list response schema",
                        "response_keys": keys,
                        "response_preview": json.dumps(response, ensure_ascii=False)[:500],
                    })
                    unsupported = True
                    failed = True
                    break
                break
            except Exception as exc:
                diagnostics.append({"endpoint": endpoint, "category": category, "page": page_no,
                                    "attempt": attempt + 1, "error": str(exc)})
                data = None
                if attempt < MAX_ATTEMPTS - 1:
                    page.wait_for_timeout(1000 * (2 ** attempt))
        if data is None:
            failed = True
            break
        if expected is None:
            expected = int(response_total) if response_total is not None and response_total >= 0 else None
        for item in data:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item.update(_category=category, _streamer=streamer)
            item["_lev"] = leverage(item)
            rows.append(item)
        # Ne jamais arrêter sur une page courte si Binance annonce encore des résultats :
        # certains endpoints renvoient des pages partielles. Pour un total connu, on s'arrête
        # seulement quand le nombre d'IDs uniques atteint le total annoncé. Sans total, une page
        # courte/ vide reste le seul signal de fin disponible.
        ids_seen = {
            str(i.get("strategyId") or i.get("algoId") or i.get("strategy_id") or "").strip()
            for i in rows
            if str(i.get("strategyId") or i.get("algoId") or i.get("strategy_id") or "").strip()
        }
        if expected is not None and len(ids_seen) >= expected:
            break
        if expected is None and len(data) < PAGE_SIZE:
            break
        if expected is not None and not data:
            # Binance annonce plus de résultats mais renvoie une page vide : conserver
            # l'échec explicite plutôt que d'affirmer une collecte exhaustive.
            failed = len(ids_seen) < expected
            break
    else:
        failed = True

    if stats is not None:
        ids = [str(i.get("strategyId") or i.get("algoId") or i.get("strategy_id") or "").strip()
               for i in rows if str(i.get("strategyId") or i.get("algoId") or i.get("strategy_id") or "").strip()]
        unique_count = len(set(ids))
        if expected is None and not failed and not rows:
            expected = 0
        stats[category] = {
            "collected": len(rows), "unique_collected": unique_count,
            "duplicate_rows": len(rows) - unique_count, "expected": expected,
            "complete": (not failed) and expected is not None and unique_count >= expected,
            "unsupported_response_schema": unsupported,
        }
    return rows


def fetch_prices(page, symbols, symbol_assets=None):
    wanted = {normalize_symbol(s) for s in symbols if str(s).strip()}
    quote_assets = {_quote_asset(s, symbol_assets) for s in wanted}
    quote_assets.discard("")
    wanted |= {q + "USDT" for q in quote_assets if q and q not in USD_STABLE}
    wanted |= {"USDT" + q for q in quote_assets if q and q not in USD_STABLE}
    if not symbol_assets:
        for quote_asset in quote_assets:
            if quote_asset and quote_asset not in USD_STABLE:
                for base in ("BTC", "ETH", "BNB", "XRP", "SOL"):
                    wanted.add(base + quote_asset)
                    wanted.add(base + "USDT")
    if symbol_assets:
        for quote_asset in quote_assets:
            if not quote_asset or quote_asset in USD_STABLE:
                continue
            markets = [
                (market, assets) for market, assets in symbol_assets.items()
                if assets.get("quoteAsset") == quote_asset and assets.get("baseAsset")
            ]
            markets.sort(key=lambda pair: (
                0 if pair[1]["baseAsset"] in {"BTC", "ETH", "BNB", "XRP", "SOL"} else 1,
                pair[0],
            ))
            for market, assets in markets[:12]:
                wanted.add(market)
                base = assets["baseAsset"]
                wanted.add(base + "USDT")
                wanted.add("USDT" + base)
    prices = {}
    endpoints = [
        "https://data-api.binance.vision/api/v3/ticker/price", "https://api-gcp.binance.com/api/v3/ticker/price",
        "https://api.binance.com/api/v3/ticker/price", "https://api1.binance.com/api/v3/ticker/price",
        "https://api2.binance.com/api/v3/ticker/price", "https://api3.binance.com/api/v3/ticker/price",
        "https://api4.binance.com/api/v3/ticker/price", "https://fapi.binance.com/fapi/v1/ticker/price",
        "https://dapi.binance.com/dapi/v1/ticker/price",
    ]

    def consume(payload):
        for row in payload if isinstance(payload, list) else []:
            symbol, price = normalize_symbol(row.get("symbol")), num(row.get("price"), None)
            if symbol in wanted and symbol not in prices and price is not None:
                prices[symbol] = price

    for endpoint in endpoints:
        if not wanted - set(prices):
            break
        try:
            response = page.request.get(endpoint, timeout=30000)
            if response.ok:
                consume(response.json())
        except Exception:
            pass
        missing = sorted(wanted - set(prices))     # repli par lots si la réponse globale est refusée
        for start in range(0, len(missing), 100):
            try:
                response = page.request.get(
                    endpoint, params={"symbols": json.dumps(missing[start:start + 100], separators=(",", ":"))},
                    timeout=30000)
                if response.ok:
                    consume(response.json())
            except Exception:
                continue
    return prices


def parse_visible_metric(body_text, label):
    text = str(body_text or "").replace("\n", " | ")
    match = re.search(re.escape(label) + r"\s*(?:\([^)]*\))?\s*(?:\|\s*)?([+-]?\d[\d,]*(?:\.\d+)?)", text, re.I)
    return num(match.group(1), None) if match else None


def capture_detail_metrics(page, rows, budget_s, now):
    """Grid Profit / Total Profit / Floating officiels lus sur la page de détail Binance.
    Texte visible prioritaire, puis JSON des endpoints de détail (listes ignorées). Bornée par `budget_s` ;
    l'ordre de visite change chaque heure pour couvrir progressivement tous les bots."""
    metrics, endpoints, samples = {}, set(), []
    stats = {"budget_s": round(budget_s), "candidates": 0, "visited": 0, "pending": 0, "captured": 0, "errors": 0}
    active = {"sid": None}

    def on_response(response):
        sid = active["sid"]
        try:
            url = response.url
            if not sid or "/bapi/" not in url or "json" not in (response.headers.get("content-type") or "").lower():
                return
            body = response.text()
            low = body.lower()
            if "gridprofit" in low or "totalprofit" in low:
                endpoints.add(url.split("?")[0])          # aide à repérer l'API directe
            if not any(t in url.lower() for t in DETAIL_ENDPOINT_TOKENS):
                return
            payload = json.loads(body)
            found = {"gridProfit": find_exact_numeric_key(payload, ["gridProfit"], lists=False),
                     "totalProfit": find_exact_numeric_key(payload, ["totalProfit"], lists=False),
                     "floatingPnl": find_exact_numeric_key(payload, FLOAT_KEYS, lists=False)}
            found = {k: v for k, v in found.items() if v is not None}
            if found:
                metrics.setdefault(sid, {}).update(found, source="BINANCE_DETAIL_API")
        except Exception:
            pass

    candidates = []
    hour = now.strftime("%Y%m%d%H")
    for row in rows:
        sid, symbol = str(row.get("strategyId") or "").strip(), str(row.get("symbol") or "").strip()
        path = next((p for name, p in DETAIL_PAGES.items() if name in str(row.get("_category") or "").lower()), None)
        matched = max(num(row.get(k), 0.0) for k in ("matchedTrades", "matchedCount", "latestMatchedCount", "totalMatchedTrades"))
        # Les bots sans activité sont des pages "Pending Trigger" (0/--) : inutile de les visiter.
        if (not sid or not symbol or not path or str(row.get("_category") or "").strip().lower() != "spot grid"
                or num(row.get("runningTime"), 0.0) <= 0 or matched <= 0 or num(row.get("pnl"), 0.0) == 0):
            continue
        candidates.append((zlib.crc32(f"{sid}:{hour}".encode()), sid, f"{path}?symbol={quote(symbol)}&strategyId={quote(sid)}"))
    candidates = list({c[1]: c for c in sorted(candidates)}.values())
    stats["candidates"] = len(candidates)

    page.on("response", on_response)
    deadline = time.monotonic() + budget_s
    try:
        for _, sid, url in candidates:
            if time.monotonic() >= deadline:
                break
            active["sid"] = sid
            stats["visited"] += 1
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:      # attend une valeur chiffrée après le libellé (et non le libellé seul)
                    page.wait_for_function(
                        r"() => /(Grid Profit|Total Profit)[^0-9+-]{0,60}[-+]?\d|Pending Trigger/i.test(document.body.innerText)",
                        timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(500)
                body_text = page.locator("body").inner_text(timeout=10000)
                if "Pending Trigger" in body_text or "Duration --" in body_text:
                    metrics.pop(sid, None)
                    stats["pending"] += 1
                    continue
                visible = {"gridProfit": parse_visible_metric(body_text, "Grid Profit"),
                           "totalProfit": parse_visible_metric(body_text, "Total Profit"),
                           "floatingPnl": parse_visible_metric(body_text, "Floating Profit")}
                visible = {k: v for k, v in visible.items() if v is not None}
                if visible:
                    metrics.setdefault(sid, {}).update(visible, source="BINANCE_DETAIL_VISIBLE")
                elif sid not in metrics and len(samples) < 3:      # preuve pour comprendre pourquoi rien n'est lu
                    samples.append({"strategyId": sid, "url": url, "login_prompt": "Log In" in body_text,
                                    "text_start": body_text[:600]})
                if stats["visited"] >= DETAIL_GIVEUP_AFTER and not metrics:
                    stats["aborted"] = f"aucune métrique lue après {stats['visited']} visites"
                    break
            except Exception as exc:
                stats["errors"] += 1
                print(f"DETAIL_PAGE_ERROR {url} {exc}")
    finally:
        active["sid"] = None
        page.remove_listener("response", on_response)
    stats["captured"] = len(metrics)
    stats["endpoints_with_profit_fields"] = sorted(endpoints)
    stats["unparsed_page_samples"] = samples
    return metrics, stats


# --------------------------------------------------------------------------- sorties
def build_current(rows, prices, symbol_assets=None):
    """Un enregistrement par strategyId : Spot Grid uniquement, sans filtre de levier ni de performance."""
    current, seen = [], set()
    for x in rows:
        sid, symbol = str(x.get("strategyId") or "").strip(), str(x.get("symbol") or "").strip()
        lev = num(x.get("_lev"), 99.0)  # conservé comme donnée descriptive, jamais utilisé comme filtre
        category = str(x.get("_category") or "").strip().lower()
        if not sid or not symbol or category != "spot grid" or sid in seen:
            continue
        seen.add(sid)
        price = first_num(x, ["currentPrice", "lastPrice", "marketPrice", "price"])
        if price is None:
            price = find_exact_numeric_key(x, ["currentPrice", "lastPrice", "marketPrice", "latestPrice", "latestMarketPrice"])
        if price is None:
            price = prices.get(normalize_symbol(symbol))
        x["_audit_current_price"] = price
        m = grid_metrics(x, prices, symbol_assets)

        def metric(*keys):
            return blank(num(next((x[k] for k in keys if k in x), None), None))

        current.append({
            "strategyId": sid, "category": x.get("_category", ""), "strategyType": x.get("strategyType", ""),
            "symbol": symbol, "leverage": lev,
            "minInvestment": x.get("minInvestment", x.get("minimumInvestment", "")),
            "runningTime": x.get("runningTime", ""),
            "roi": metric("roi", "roiPct", "roiRate"), "pnl": metric("pnl", "profitLoss", "totalPnl"),
            "matchedTrades": metric("matchedTrades", "matchedCount", "totalMatchedTrades"),
            "mdd7d": metric("mdd7d", "sevenDayMdd", "7dMdd"),
            "gridProfit": blank(m["gridProfit"]), "gridProfitSource": m["gridSource"],
            "gridMode": m["gridMode"], "gridCount": blank(m["gridCount"]),
            "qtyPerOrderEstimate": blank(m["qty"]), "qtyPerOrderSource": m["qtySource"],
            "gridProfitEstimateLow": blank(m["low"]), "gridProfitEstimateMid": blank(m["mid"]),
            "gridProfitEstimateHigh": blank(m["high"]),
            "totalProfit": blank(m["totalProfit"]), "totalProfitSource": m["totalSource"],
            "gridProfitTotalProfitRatio": m["ratio"], "gridProfitTotalProfitRatioSource": m["ratioSource"],
            "gridProfitTotalProfitRatioEstimateLow": blank(m["ratioLow"]),
            "gridProfitTotalProfitRatioEstimateMid": blank(m["ratioMid"]),
            "gridProfitTotalProfitRatioEstimateHigh": blank(m["ratioHigh"]),
            "gridProfitTotalProfitRatioStatus": m["ratioStatus"],
            "floatingProfit": blank(m["floating"]), "floatingProfitSource": m["floatingBasis"],
            "gridProfitEstimateMethod": m["method"], "gridProfitConfidence": m["confidence"],
            "gridProfitEstimateStatus": {"BINANCE_EXACT": "EXACT", "RECONSTRUCTED": "ESTIMATED_NOT_EXACT"}.get(m["gridSource"], ""),
            "currentPrice": fmt_price(price), "priceRange": m["priceRange"],
            "profitPerGridAfterFees": m["profitPerGrid"],
        })
    return current


def write_csv(path, current, opener=open):
    with opener(path, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(current)


def write_history(current, now):
    """Un instantané gzip par scan horaire ; purge au-delà de RETENTION_DAYS."""
    folder = HISTORY / now.strftime("%Y/%m/%d")
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = folder / f"{now.strftime('%Y%m%dT%H%M%SZ')}.csv.gz"
    write_csv(snapshot, current, opener=lambda p, mode, **kw: gzip.open(p, mode, compresslevel=9, **kw))
    cutoff, removed = now - dt.timedelta(days=RETENTION_DAYS), 0
    for path in HISTORY.rglob("*.csv.gz"):
        try:
            stamp = dt.datetime.strptime(path.name[:16], "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if stamp < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    for directory in sorted(HISTORY.glob("*/*/*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    return snapshot, removed


def main():
    from playwright.sync_api import sync_playwright     # import local : les tests n'en ont pas besoin

    start, now = time.monotonic(), dt.datetime.now(dt.timezone.utc)
    OUT.mkdir(parents=True, exist_ok=True)
    HISTORY.mkdir(parents=True, exist_ok=True)
    diagnostics, stats = [], {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        rows = pages(page, TOP, {"strategyType": 1, "symbol": "", "zone": "", "sort": "pnl"},
                     "Spot Grid", "SPOT_GRID", diagnostics, stats)
        symbol_assets = fetch_exchange_info(page)
        prices = fetch_prices(page, [r.get("symbol") for r in rows], symbol_assets)
        budget = max(0.0, min(DETAIL_BUDGET_S, RUN_BUDGET_S - (time.monotonic() - start)))
        detail, detail_stats = capture_detail_metrics(page, rows, budget, now)
        browser.close()

    for row in rows:
        metrics = detail.get(str(row.get("strategyId") or "").strip())
        if metrics:
            row["_detail_grid_profit"] = metrics.get("gridProfit")
            row["_detail_total_profit"] = metrics.get("totalProfit")
            row["_detail_floating_pnl"] = metrics.get("floatingPnl")

    current = build_current(rows, prices, symbol_assets)
    write_csv(OUT / "current_multicriteria.csv", current)
    snapshot, removed = write_history(current, now)

    incomplete = sorted(name for name, s in stats.items() if not s["complete"])
    exact = sum(1 for r in current if r["gridProfitSource"] == "BINANCE_EXACT")
    meta = {
        "scan_utc": now.isoformat(), "raw_rows": len(rows), "spot_grid_bots": len(current),
        "collection_complete": not incomplete, "incomplete_categories": incomplete, "collection": stats,
        "grid_profit_exact": exact, "detail_capture": detail_stats,
        "conversion": {
            "exchange_info_markets": len(symbol_assets),
            "quote_assets_seen": sorted({_quote_asset(r.get("symbol"), symbol_assets) for r in rows}),
            "unresolved_quote_assets": sorted({_quote_asset(r.get("symbol"), symbol_assets) for r in rows
                                               if _quote_asset(r.get("symbol"), symbol_assets)
                                               and _quote_usd_price(r.get("symbol"), prices, symbol_assets) is None}),
        },
        "endpoint_errors": diagnostics[:50], "snapshot": str(snapshot), "old_snapshots_removed": removed,
        "profit_per_grid_fee_reference": f"{FEE:.2%} par ordre (Spot) ; les frais réels varient selon paire / VIP / BNB.",
        "strategy_type_probe_max": MAX_STRATEGY_TYPE,
        "limitations": ["Tri Marketplace par PNL : un re-tri pendant la pagination peut sauter ou dupliquer des lignes.",
                        "La collecte publique ne garantit pas les bots privés/arrêtés ni les familles sans endpoint public identifié.",
                        "Tout strategyType dont la réponse BAPI ne contient pas une liste reconnue reste incomplet, jamais supposé vide."],
    }
    (OUT / "scan_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"raw_rows={len(rows)} spot_grid={len(current)} complete={not incomplete} grid_profit_exact={exact} "
          f"detail={detail_stats['captured']}/{detail_stats['visited']} visités (candidats {detail_stats['candidates']}, "
          f"pending {detail_stats['pending']}, erreurs {detail_stats['errors']}"
          f"{', ABANDON: ' + detail_stats['aborted'] if 'aborted' in detail_stats else ''})")
    for name, s in stats.items():
        if not s["complete"]:
            print(f"::warning::{name}: collecte={s['collected']} uniques={s['unique_collected']} attendu={s['expected']}")
    print(f"DONE snapshot={snapshot} removed={removed}")


if __name__ == "__main__":
    main()