#!/usr/bin/env python3
"""src/build_multicriteria.py -- étape 3 : construit results/current_multicriteria.csv.

Le Grid Profit officiel est conservé lorsqu'il est lu chez Binance ; sinon les estimations Low/Mid/High
restent explicitement séparées. Par convention d'audit, floatingProfit est estimé avec le milieu :
Total Profit - gridProfitEstimateMid lorsque le Grid Profit officiel n'est pas disponible.
"""
import os
import re
import sys

import numpy as np
import pandas as pd

COLUMNS = [
    "strategyId", "category", "strategyType", "symbol", "leverage",
    "minInvestment", "runningTime", "runningTime en J",
    "pnl", "roi (fourni par Binance)", "roi (calculé)", "matchedTrades",
    "Trades / J", "mdd7d", "gridProfit", "gridMode", "gridCount",
    "qtyPerOrderEstimate", "gridProfitEstimateLow", "gridProfitEstimateMid",
    "gridProfitEstimateHigh", "totalProfit", "gridProfitTotalProfitRatio",
    "floatingProfit", "currentPrice", "priceRangeLow", "priceRangeHigh", "Niveau",
    "profitPerGridAfterFeesLow", "profitPerGridAfterFeesMid", "profitPerGridAfterFeesHigh",
]


def main(src, dst):
    df = pd.read_csv(src, dtype={"strategyId": str})
    if df.empty:
        raise SystemExit(f"Source vide : {src}")
    missing = [c for c in ("strategyId", "leverage", "symbol") if c not in df]
    if missing:
        raise SystemExit(f"Colonnes indispensables absentes : {missing}")

    df = df[df["strategyId"].notna() & df["symbol"].notna()].copy()
    if "category" not in df:
        raise SystemExit("Colonne category absente : impossible de limiter le CSV à Spot Grid")
    df = df[df["category"].astype(str).str.strip().str.lower().eq("spot grid")].drop_duplicates("strategyId").copy()
    if df.empty:
        raise SystemExit("Aucun bot Spot Grid dans la source")

    def col(name):
        return pd.to_numeric(df[name], errors="coerce") if name in df else pd.Series(np.nan, index=df.index)

    lev, runtime, roi, pnl = col("leverage"), col("runningTime"), col("roi"), col("pnl")
    min_inv, matched, mdd, price = col("minInvestment"), col("matchedTrades"), col("mdd7d"), col("currentPrice")
    # Si Binance ne fournit aucun minimum exploitable (vide, nul ou négatif), utiliser 1 comme base conventionnelle.
    # Cela permet de calculer le ROI indicatif PNL / capital * 100 sans diviser par zéro.
    min_inv = min_inv.where(min_inv.gt(0), 1.0)
    days = (runtime / 86400).where(runtime >= 0)
    trades_day = (matched / days).where(days > 0)
    total = col("totalProfit").where(col("totalProfit").notna(), pnl)

    # ROI indicatif : ne jamais supprimer un résultat calculable à cause de son écart au ROI Binance.
    # Il est renseigné dès que PNL est numérique et que le capital minimum est strictement positif.
    roi_calc = (pnl / min_inv * 100).where(pnl.notna() & min_inv.gt(0))

    # Grid Profit exact (Binance) ou estimation (Low / Mid / High).
    source = df["gridProfitSource"].fillna("") if "gridProfitSource" in df else pd.Series("", index=df.index)
    exact = source.eq("BINANCE_EXACT") & col("gridProfit").notna()
    gp_exact = col("gridProfit").where(exact)
    low, mid, high, qty = (col(c) for c in ("gridProfitEstimateLow", "gridProfitEstimateMid",
                                            "gridProfitEstimateHigh", "qtyPerOrderEstimate"))
    interval_ok = low.notna() & mid.notna() & high.notna() & (low <= mid + 1e-8) & (mid <= high + 1e-8)
    for s in (low, mid, high, qty):
        s[~(exact | interval_ok)] = np.nan

    # Ratio : exact si Grid Profit exact, sinon ratio central estimé. Le CSV reste numérique, sans préfixe.
    nonzero = total.where(total != 0)
    ratio_exact = (gp_exact / nonzero).where(exact)
    ratio_est = (mid / nonzero).where(~exact & interval_ok)
    ratio = ratio_exact.where(exact, ratio_est)
    ratio_txt = [
        np.nan if pd.isna(v) else float(f"{v:.6f}")
        for i, v in ratio.items()
    ]

    # Convention retenue : floatingProfit estimé avec le Grid Profit central.
    floating_exact = (total - gp_exact).where(exact & total.notna())
    floating_est = (total - mid).where(~exact & interval_ok & total.notna())
    floating = floating_exact.where(exact, floating_est)
    # Sépare les bornes de la plage de prix et situe le cours courant entre ces bornes.
    def parse_range(value):
        if pd.isna(value):
            return (np.nan, np.nan)
        numbers = re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", str(value).replace(",", "."))
        if len(numbers) != 2:
            return (np.nan, np.nan)
        lo, hi = float(numbers[0]), float(numbers[1])
        return (lo, hi) if lo <= hi else (hi, lo)

    ranges = (df["priceRange"].map(parse_range) if "priceRange" in df
              else pd.Series([(np.nan, np.nan)] * len(df), index=df.index))
    range_low = ranges.map(lambda pair: pair[0])
    range_high = ranges.map(lambda pair: pair[1])

    def price_level(current, lo, hi):
        if pd.isna(current) or pd.isna(lo) or pd.isna(hi) or hi <= lo:
            return ""
        pct = (float(current) - float(lo)) / (float(hi) - float(lo)) * 100.0
        if pct < 0:
            return f"Hors plage — {pct:.1f}% (sous la borne basse)"
        if pct > 100:
            return f"Hors plage — {pct:.1f}% (au-dessus de la borne haute)"
        if abs(pct - 50.0) < 1e-9:
            label = "Neutre"
        else:
            label = "Bullish" if pct > 50 else "Bearish"
        return f"{label} — {pct:.1f}%"

    level = pd.Series(
        [price_level(p, lo, hi) for p, lo, hi in zip(price, range_low, range_high)],
        index=df.index, dtype=object
    )

    # Une valeur unique va dans Mid ; une fourchette arithmétique est répartie en Low/Mid/High.
    def parse_profit_per_grid(value):
        if pd.isna(value):
            return (np.nan, np.nan, np.nan)
        numbers = re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", str(value).replace(",", "."))
        if len(numbers) == 1:
            return (np.nan, float(numbers[0]), np.nan)
        if len(numbers) == 2:
            lo, hi = sorted(float(n) for n in numbers)
            return (lo, (lo + hi) / 2.0, hi)
        return (np.nan, np.nan, np.nan)

    ppg = (df["profitPerGridAfterFees"].map(parse_profit_per_grid)
           if "profitPerGridAfterFees" in df else pd.Series([(np.nan, np.nan, np.nan)] * len(df), index=df.index))
    ppg_low = ppg.map(lambda v: v[0])
    ppg_mid = ppg.map(lambda v: v[1])
    ppg_high = ppg.map(lambda v: v[2])

    floating_source = pd.Series("", index=df.index, dtype=object)
    floating_source.loc[exact & total.notna()] = "BINANCE_TOTAL_MINUS_EXACT_GRID_PROFIT"
    floating_source.loc[~exact & interval_ok & total.notna()] = "ESTIMATED_TOTAL_MINUS_MID_GRID_PROFIT"

    out = pd.DataFrame({
        "strategyId": df.strategyId, "category": df.get("category"),
        "strategyType": df.get("strategyType"), "symbol": df.symbol, "leverage": lev,
        "minInvestment": min_inv, "runningTime": runtime, "runningTime en J": days,
        "pnl": pnl, "roi (fourni par Binance)": roi, "roi (calculé)": roi_calc, "matchedTrades": matched,
        "Trades / J": trades_day, "mdd7d": mdd, "gridProfit": gp_exact, "gridMode": df.get("gridMode"),
        "gridCount": col("gridCount"), "qtyPerOrderEstimate": qty, "gridProfitEstimateLow": low,
        "gridProfitEstimateMid": mid, "gridProfitEstimateHigh": high, "totalProfit": total,
        "gridProfitTotalProfitRatio": ratio_txt, "floatingProfit": floating,
        "floatingProfitSource": floating_source, "currentPrice": price,
        "priceRangeLow": range_low, "priceRangeHigh": range_high, "Niveau": level,
        "profitPerGridAfterFeesLow": ppg_low, "profitPerGridAfterFeesMid": ppg_mid,
        "profitPerGridAfterFeesHigh": ppg_high,
    })[COLUMNS]
    out = out.assign(_k=pd.to_numeric(out.strategyId, errors="coerce")) \
             .sort_values(["_k", "strategyId"], kind="stable").drop(columns="_k").reset_index(drop=True)

    folder = os.path.dirname(dst) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = dst + ".tmp"
    out.to_csv(tmp, index=False)
    os.replace(tmp, dst)

    with open(os.path.join(folder, "current_multicriteria_summary.txt"), "w", encoding="utf-8") as h:
        h.write(f"COUNT={len(out)}\n"
                "UNIVERSE=Binance public Spot Grid bots only; no leverage filter; verify results/scan_meta.json "
                "collection_complete before treating the scan as exhaustive; no age/price-range/profit-per-grid filters\n"
                "ROI_CALCULATED=PNL / minInvestment * 100; missing, zero or negative minInvestment is replaced with 1; blank only if PNL is unavailable\n"
                "FLOATING_PROFIT=exact Total Profit minus exact Grid Profit when available; otherwise estimated as "
                "Total Profit minus gridProfitEstimateMid when Grid Profit is estimated\n"
                "RATIO=numeric without approximation prefix for both exact and estimated values; "
                "empty when Total Profit is zero; no 100%/500% cap\n")
    print(f"{len(out)} stratégies -> {dst} | Grid Profit exact : {int(exact.sum())} | "
          f"floatingProfit estimé : {int(floating_est.notna().sum())} | ratio estimé : {int(ratio_est.notna().sum())}")


if __name__ == "__main__":
    s = sys.argv[1] if len(sys.argv) > 1 else "results/current_multicriteria.csv"
    main(s, sys.argv[2] if len(sys.argv) > 2 else s)
