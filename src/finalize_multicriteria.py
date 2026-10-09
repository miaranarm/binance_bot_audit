#!/usr/bin/env python3
"""Finalise l'export public Binance en un CSV strict et classé pour l'analyse."""
import csv, math, re
from pathlib import Path

SRC = Path("results/current_multicriteria.csv")
TMP = Path("results/current_multicriteria.final.tmp.csv")
FIELDS = [
    "rank", "strategyId", "category", "strategyType", "symbol", "leverage",
    "minInvestment", "runningTime", "runningTime en J",
    "roi (fourni par Binance)", "roi (calculé)", "pnl", "matchedTrades",
    "Trades / J", "mdd7d", "gridProfit", "gridMode", "gridCount",
    "qtyPerOrderEstimate", "gridProfitEstimateLow", "gridProfitEstimateMid",
    "gridProfitEstimateHigh", "totalProfit", "gridProfitTotalProfitRatio",
    "floatingProfit", "currentPrice", "priceRange", "profitPerGridAfterFees", "score",
]

def number(v, default=None):
    try:
        if v is None or str(v).strip() == "":
            return default
        x = float(str(v).strip().replace(",", "").replace("%", ""))
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default

def ratio_value(row):
    # Exact Binance/detail ratio wins; otherwise use the existing documented reconstruction.
    direct = number(row.get("gridProfitTotalProfitRatio"))
    if direct is not None:
        return direct
    gp = number(row.get("gridProfit"))
    total = number(row.get("totalProfit"))
    return gp / total if gp is not None and total not in (None, 0) else None

def range_bounds(value):
    if not value:
        return None, None
    parts = re.split(r"\s+-\s+", str(value).strip(), maxsplit=1)
    if len(parts) != 2:
        return None, None
    return number(parts[0]), number(parts[1])

def profit_grid_min(value):
    if not value:
        return None
    vals = re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)", str(value).replace(",", ""))
    vals = [number(v) for v in vals]
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None

def percentile(values, value, reverse=False):
    vals = sorted(v for v in values if v is not None)
    if value is None or not vals:
        return 0.0
    if len(vals) == 1:
        return 50.0
    # Percentile rank robust to outliers; reverse for risk metrics.
    less = sum(v <= value for v in vals)
    score = 100.0 * less / len(vals)
    return 100.0 - score if reverse else score

def main():
    if not SRC.exists() or SRC.stat().st_size == 0:
        raise SystemExit(f"Source manquante/vide: {SRC}")
    with SRC.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit("Le CSV source ne contient aucune stratégie.")

    # Fail closed on unknown leverage. Keep only unique public strategies <= 1x.
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
        # This is a distinct calculation against Binance's minimumInvestment field;
        # it is not presented as Binance ROI because minimum investment may not be actual capital.
        roi_calc = pnl / min_inv * 100.0 if pnl is not None and min_inv is not None and min_inv > 0 else None
        row["runningTime en J"] = days
        row["roi (fourni par Binance)"] = roi_binance
        row["roi (calculé)"] = roi_calc
        row["Trades / J"] = trades / days if trades is not None and days is not None and days > 0 else None

        # Ensure the estimate midpoint is the same value used for the headline Grid Profit.
        gp = number(row.get("gridProfit"))
        gp_low = number(row.get("gridProfitEstimateLow"))
        gp_mid = number(row.get("gridProfitEstimateMid"))
        gp_high = number(row.get("gridProfitEstimateHigh"))
        source = str(row.get("gridProfitSource") or "")
        if gp_mid is None and gp is not None and source in {"RECONSTRUCTED", "BINANCE_EXACT"}:
            gp_mid = gp
        if source == "BINANCE_EXACT" and gp is not None:
            gp_low = gp if gp_low is None else gp_low
            gp_mid = gp if gp_mid is None else gp_mid
            gp_high = gp if gp_high is None else gp_high
        if gp is None and gp_mid is not None:
            gp = gp_mid
        row["gridProfit"] = gp
        row["gridProfitEstimateLow"] = gp_low
        row["gridProfitEstimateMid"] = gp_mid
        row["gridProfitEstimateHigh"] = gp_high
        row["gridProfitTotalProfitRatio"] = ratio_value(row)

        # Do not invent a total or floating PnL when Binance has not exposed enough data.
        total = number(row.get("totalProfit"))
        floating = number(row.get("floatingProfit"))
        floating_source = str(row.get("floatingProfitSource") or "")
        # A residual based on an estimated Grid Profit is not Floating Profit.
        # Keep the field blank unless its basis is exact/official.
        if "RECONSTRUCTED" in floating_source or source == "RECONSTRUCTED":
            floating = None
        elif floating is None and total is not None and gp is not None and source == "BINANCE_EXACT":
            floating = total - gp
        row["floatingProfit"] = floating

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

    # Score aligned with the user's priorities: Grid/Total ratio, healthy grid economics,
    # price near the centre of range, sustained runtime/activity, then drawdown.
    for row in enriched:
        ratio = row["_ratio"]
        # Ratio is meaningful for ranking only when Total Profit is positive; don't reward
        # pathological ratios caused by zero/negative totals.
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

    # Keep the human-readable summary consistent with the final ranking rather
    # than leaving the preliminary ROI/PNL ranking produced by audit_all.py.
    summary = Path("results/current_multicriteria_summary.txt")
    with summary.open("w", encoding="utf-8") as handle:
        handle.write(f"COUNT={len(enriched)}\\n")
        handle.write("UNIVERSE=Binance public Bot Marketplace\\n")
        handle.write("FILTER=unique strategyId; known leverage <= 1; non-empty symbol\\n")
        handle.write("SCORING=GRID_TOTAL_RATIO35 GRID_NET_PER_GRID15 PRICE_RANGE_CENTRE15 RUNTIME10 TRADES_PER_DAY10 GRID_PROFIT_PER_TRADE5 MDD7D10\\n")
        for rank, row in enumerate(enriched[:20], 1):
            handle.write(
                f"{rank}. {row.get('strategyId', '')} {row.get('symbol', '')} "
                f"score={row.get('score', 0):.4f} "
                f"gridProfit={row.get('gridProfit', '')} "
                f"ratio={row.get('gridProfitTotalProfitRatio', '')} "
                f"roiBinance={row.get('roi (fourni par Binance)', '')} "
                f"pnl={row.get('pnl', '')} tradesPerDay={row.get('Trades / J', '')} "
                f"mdd7d={row.get('mdd7d', '')}\\n"
            )
    print(f"CSV strict finalisé: {len(enriched)} bots uniques, levier <= 1; {len(FIELDS)} colonnes.")
    print("Scores: ratio Grid/Total 35%, profit/grille 15%, prix centré 15%, durée 10%, activité 10%, profit/trade 5%, MDD 10%.")

if __name__ == "__main__":
    main()
