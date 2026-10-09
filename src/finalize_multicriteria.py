#!/usr/bin/env python3
"""Finalize the public Binance export while retaining Claude's Grid Profit estimates.

The headline gridProfit remains populated when a reconstructed midpoint exists,
but gridProfitSource and gridProfitEstimateStatus clearly distinguish it from
an exact Binance value. A residual floatingProfit based on an estimate is also
labelled as reconstructed, never as an official Binance metric.
"""
import csv, math, re
from pathlib import Path

SRC = Path("results/current_multicriteria.csv")
TMP = Path("results/current_multicriteria.final.tmp.csv")
FIELDS = [
    "rank", "strategyId", "category", "strategyType", "symbol", "leverage",
    "minInvestment", "runningTime", "runningTime en J",
    "roi (fourni par Binance)", "roi (calculé)", "pnl", "matchedTrades",
    "Trades / J", "mdd7d", "gridProfit", "gridProfitSource", "gridProfitEstimateStatus",
    "gridMode", "gridCount", "qtyPerOrderEstimate", "gridProfitEstimateLow",
    "gridProfitEstimateMid", "gridProfitEstimateHigh", "totalProfit",
    "gridProfitTotalProfitRatio", "floatingProfit", "floatingProfitSource",
    "currentPrice", "priceRange", "profitPerGridAfterFees", "score",
]

def number(v, default=None):
    try:
        if v is None or str(v).strip() == "":
            return default
        x = float(str(v).strip().replace(",", "").replace("%", ""))
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default

def ratio_value(row, grid_value=None):
    direct = number(row.get("gridProfitTotalProfitRatio"))
    if direct is not None:
        return direct
    total = number(row.get("totalProfit"))
    gp = grid_value if grid_value is not None else number(row.get("gridProfit"))
    if total in (None, 0) or gp is None:
        return None
    return gp / total

def range_bounds(value):
    if not value:
        return None, None
    parts = re.split(r"\\s+-\\s+", str(value).strip(), maxsplit=1)
    if len(parts) != 2:
        return None, None
    return number(parts[0]), number(parts[1])

def profit_grid_min(value):
    if not value:
        return None
    vals = re.findall(r"[-+]?(?:\\d+\\.?\\d*|\\.\\d+)", str(value).replace(",", ""))
    vals = [number(v) for v in vals]
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None

def percentile(values, value, reverse=False):
    vals = sorted(v for v in values if v is not None)
    if value is None or not vals:
        return 0.0
    if len(vals) == 1:
        return 50.0
    score = 100.0 * sum(v <= value for v in vals) / len(vals)
    return 100.0 - score if reverse else score

def main():
    if not SRC.exists() or SRC.stat().st_size == 0:
        raise SystemExit(f"Source manquante/vide: {SRC}")
    with SRC.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit("Le CSV source ne contient aucune stratégie.")

    unique = {}
    for row in rows:
        sid = str(row.get("strategyId") or "").strip()
        lev = number(row.get("leverage"))
        symbol = str(row.get("symbol") or "").strip()
        if not sid or not symbol or lev is None or lev > 1.0:
            continue
        unique.setdefault(sid, row)
    rows = list(unique.values())

    enriched = []
    for row in rows:
        runtime = number(row.get("runningTime"))
        days = runtime / 86400.0 if runtime is not None and runtime >= 0 else None
        trades = number(row.get("matchedTrades"))
        pnl = number(row.get("pnl"))
        min_inv = number(row.get("minInvestment"))
        roi_binance = number(row.get("roi"))
        roi_calc = pnl / min_inv * 100.0 if pnl is not None and min_inv is not None and min_inv > 0 else None
        row["runningTime en J"] = days
        row["roi (fourni par Binance)"] = roi_binance
        row["roi (calculé)"] = roi_calc
        row["Trades / J"] = trades / days if trades is not None and days is not None and days > 0 else None

        gp_exact_or_existing = number(row.get("gridProfit"))
        gp_low = number(row.get("gridProfitEstimateLow"))
        gp_mid = number(row.get("gridProfitEstimateMid"))
        gp_high = number(row.get("gridProfitEstimateHigh"))
        source = str(row.get("gridProfitSource") or "").strip()

        if source == "BINANCE_EXACT" and gp_exact_or_existing is not None:
            gp = gp_exact_or_existing
            gp_low = gp if gp_low is None else gp_low
            gp_mid = gp if gp_mid is None else gp_mid
            gp_high = gp if gp_high is None else gp_high
            status = "EXACT"
        elif source == "RECONSTRUCTED":
            gp = gp_mid if gp_mid is not None else gp_exact_or_existing
            if gp_mid is None:
                gp_mid = gp
            status = "ESTIMATED_NOT_EXACT" if gp is not None else "UNAVAILABLE"
        else:
            gp = gp_exact_or_existing
            status = "UNAVAILABLE" if gp is None else "UNVERIFIED_SOURCE"

        row["gridProfit"] = gp
        row["gridProfitSource"] = source
        row["gridProfitEstimateStatus"] = status
        row["gridProfitEstimateLow"] = gp_low
        row["gridProfitEstimateMid"] = gp_mid
        row["gridProfitEstimateHigh"] = gp_high
        row["gridProfitTotalProfitRatio"] = ratio_value(row, gp)

        total = number(row.get("totalProfit"))
        if total is None:
            total = number(row.get("pnl"))
            if total is not None:
                row["totalProfit"] = total
                if not row.get("totalProfitSource"):
                    row["totalProfitSource"] = "BINANCE_MARKETPLACE_PNL_FALLBACK"
        if gp is not None and total is not None:
            row["floatingProfit"] = total - gp
            row["floatingProfitSource"] = (
                "TOTAL_MINUS_EXACT_GRID_PROFIT" if status == "EXACT"
                else "RECONSTRUCTED_RESIDUAL_NOT_OFFICIAL"
            )
        else:
            row["floatingProfit"] = None
            row["floatingProfitSource"] = ""

        lo, hi = range_bounds(row.get("priceRange"))
        price = number(row.get("currentPrice"))
        if lo is not None and hi is not None and price is not None and hi > lo:
            row["_range_position_score"] = max(0.0, 100.0 * (1.0 - abs(2.0 * ((price-lo)/(hi-lo)) - 1.0))) if lo <= price <= hi else 0.0
        else:
            row["_range_position_score"] = None
        row["_profit_grid_min"] = profit_grid_min(row.get("profitPerGridAfterFees"))
        row["_runtime_days"] = days
        row["_trades_day"] = number(row.get("Trades / J"))
        row["_gp_per_trade"] = gp / trades if gp is not None and trades is not None and trades > 0 else None
        row["_ratio"] = number(row.get("gridProfitTotalProfitRatio"))
        row["_mdd"] = number(row.get("mdd7d"))
        enriched.append(row)

    for row in enriched:
        ratio = row["_ratio"]
        ratio_score = min(100.0, max(0.0, ratio * 100.0)) if ratio is not None and number(row.get("totalProfit"), 0) > 0 else 0.0
        pg = row["_profit_grid_min"]
        pg_score = 100.0 if pg is not None and pg >= 1.5 else (max(0.0, pg / 1.5 * 100.0) if pg is not None else 0.0)
        centre = row["_range_position_score"] or 0.0
        runtime_score = percentile([x["_runtime_days"] for x in enriched], row["_runtime_days"])
        activity_score = percentile([x["_trades_day"] for x in enriched], row["_trades_day"])
        consistency_score = percentile([x["_gp_per_trade"] for x in enriched], row["_gp_per_trade"])
        mdd = row["_mdd"]
        risk_score = percentile([x["_mdd"] for x in enriched], mdd, reverse=True) if mdd is not None else 0.0
        row["score"] = round(
            0.35 * ratio_score + 0.15 * pg_score + 0.15 * centre +
            0.10 * runtime_score + 0.10 * activity_score +
            0.05 * consistency_score + 0.10 * risk_score, 4
        )

    enriched.sort(key=lambda x: x["score"], reverse=True)
    with TMP.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for rank, row in enumerate(enriched, 1):
            out = {"rank": rank}
            for field in FIELDS:
                if field == "rank":
                    continue
                value = row.get(field, "")
                out[field] = "" if value is None else value
            writer.writerow(out)
    TMP.replace(SRC)

    summary = Path("results/current_multicriteria_summary.txt")
    with summary.open("w", encoding="utf-8") as handle:
        handle.write(f"COUNT={len(enriched)}\\n")
        handle.write("UNIVERSE=Binance public Bot Marketplace\\n")
        handle.write("FILTER=unique strategyId; known leverage <= 1; non-empty symbol\\n")
        handle.write("SCORING=GRID_TOTAL_RATIO35 GRID_NET_PER_GRID15 PRICE_RANGE_CENTRE15 RUNTIME10 TRADES_PER_DAY10 GRID_PROFIT_PER_TRADE5 MDD7D10\\n")
        for rank, row in enumerate(enriched[:20], 1):
            handle.write(
                f"{rank}. {row.get('strategyId', '')} {row.get('symbol', '')} "
                f"score={row.get('score', 0):.4f} gridProfit={row.get('gridProfit', '')} "
                f"gridProfitSource={row.get('gridProfitSource', '')} "
                f"ratio={row.get('gridProfitTotalProfitRatio', '')} "
                f"roiBinance={row.get('roi (fourni par Binance)', '')} "
                f"pnl={row.get('pnl', '')} tradesPerDay={row.get('Trades / J', '')} "
                f"mdd7d={row.get('mdd7d', '')}\\n"
            )
    print(f"CSV finalisé: {len(enriched)} bots; gridProfit exact={sum(r['gridProfitEstimateStatus']=='EXACT' for r in enriched)}, estimé={sum(r['gridProfitEstimateStatus']=='ESTIMATED_NOT_EXACT' for r in enriched)}.")
    print("Les estimations restent identifiées par leur source; elles ne sont pas présentées comme des chiffres officiels Binance.")

if __name__ == "__main__":
    main()
