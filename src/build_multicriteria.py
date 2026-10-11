#!/usr/bin/env python3
"""src/build_multicriteria.py -- étape 3 : construit results/current_multicriteria.csv.

Le Grid Profit officiel est conservé lorsqu'il est lu chez Binance ; sinon les estimations Low/Mid/High
restent explicitement séparées. Par convention d'audit, floatingProfit est estimé avec le milieu :
Total Profit - gridProfitEstimateMid. La colonne floatingProfitSource distingue toujours l'estimation
d'un calcul reposant sur un Grid Profit officiel.
"""
import os
import sys

import numpy as np
import pandas as pd

COLUMNS = [
    "strategyId", "category", "strategyType", "symbol", "leverage",
    "minInvestment", "runningTime", "runningTime en J",
    "roi (fourni par Binance)", "roi (calculé)", "pnl", "matchedTrades",
    "Trades / J", "mdd7d", "gridProfit", "gridMode", "gridCount",
    "qtyPerOrderEstimate", "gridProfitEstimateLow", "gridProfitEstimateMid",
    "gridProfitEstimateHigh", "totalProfit", "gridProfitTotalProfitRatio",
    "floatingProfit", "floatingProfitSource", "currentPrice", "priceRange",
    "profitPerGridAfterFees",
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

    # Ratio : exact si Grid Profit exact, sinon ratio central estimé (marqué ≈).
    nonzero = total.where(total != 0)
    ratio_exact = (gp_exact / nonzero).where(exact)
    ratio_est = (mid / nonzero).where(~exact & interval_ok)
    ratio = ratio_exact.where(exact, ratio_est)
    ratio_txt = [
        np.nan if pd.isna(v) else (float(f"{v:.6f}") if exact.loc[i] else f"≈{v:.6f}")
        for i, v in ratio.items()
    ]

    # Convention retenue : floatingProfit estimé avec le Grid Profit central.
    floating_exact = (total - gp_exact).where(exact & total.notna())
    floating_est = (total - mid).where(~exact & interval_ok & total.notna())
    floating = floating_exact.where(exact, floating_est)
    floating_source = pd.Series("", index=df.index, dtype=object)
    floating_source.loc[exact & total.notna()] = "BINANCE_TOTAL_MINUS_EXACT_GRID_PROFIT"
    floating_source.loc[~exact & interval_ok & total.notna()] = "ESTIMATED_TOTAL_MINUS_MID_GRID_PROFIT"

    out = pd.DataFrame({
        "strategyId": df.strategyId, "category": df.get("category"),
        "strategyType": df.get("strategyType"), "symbol": df.symbol, "leverage": lev,
        "minInvestment": min_inv, "runningTime": runtime, "runningTime en J": days,
        "roi (fourni par Binance)": roi, "roi (calculé)": roi_calc, "pnl": pnl, "matchedTrades": matched,
        "Trades / J": trades_day, "mdd7d": mdd, "gridProfit": gp_exact, "gridMode": df.get("gridMode"),
        "gridCount": col("gridCount"), "qtyPerOrderEstimate": qty, "gridProfitEstimateLow": low,
        "gridProfitEstimateMid": mid, "gridProfitEstimateHigh": high, "totalProfit": total,
        "gridProfitTotalProfitRatio": ratio_txt, "floatingProfit": floating,
        "floatingProfitSource": floating_source, "currentPrice": price,
        "priceRange": df.get("priceRange"), "profitPerGridAfterFees": df.get("profitPerGridAfterFees"),
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
                "ROI_CALCULATED=PNL / positive minInvestment * 100 whenever both values are valid; never filtered by comparison with Binance ROI\n"
                "FLOATING_PROFIT=exact Total Profit minus exact Grid Profit when available; otherwise estimated as "
                "Total Profit minus gridProfitEstimateMid; see floatingProfitSource\n"
                "RATIO=exact numeric when Grid Profit is exact; estimated ratio prefixed with ≈; "
                "empty when Total Profit is zero; no 100%/500% cap\n")
    print(f"{len(out)} stratégies -> {dst} | Grid Profit exact : {int(exact.sum())} | "
          f"floatingProfit estimé : {int(floating_est.notna().sum())} | ratio estimé : {int(ratio_est.notna().sum())}")


if __name__ == "__main__":
    s = sys.argv[1] if len(sys.argv) > 1 else "results/current_multicriteria.csv"
    main(s, sys.argv[2] if len(sys.argv) > 2 else s)
