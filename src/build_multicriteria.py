#!/usr/bin/env python3
"""src/build_multicriteria.py -- étape 3 : réécrit results/current_multicriteria.csv avec les 29 colonnes figées.

Lit le CSV brut de src/audit_all.py (avec provenance) et le réécrit en place. Le brut complet reste
archivé chaque heure dans results/history/*.csv.gz. Colonnes rank et score : conservées, toujours vides
(le classement n'a pas d'importance). Liste triée par strategyId (ordre stable).

Lecture du fichier final (aucune colonne de provenance) :
  gridProfit rempli  -> valeur EXACTE lue chez Binance
  gridProfit vide    -> estimation uniquement, dans Low / Mid / High
  ratio              -> numérique si exact ; préfixé "≈" si estimé (ratio central) ; vide si Total Profit = 0
  floatingProfit     -> vide dès que le Grid Profit n'est pas exact
Aucune ligne n'est supprimée pour l'âge, le prix hors plage ou le profit/grille ; une donnée incohérente
neutralise uniquement le calcul concerné.
"""
import os
import sys

import numpy as np
import pandas as pd

ROI_RATIO_MIN, ROI_RATIO_MAX = 0.5, 10

COLUMNS = [
    "rank", "strategyId", "category", "strategyType", "symbol", "leverage",
    "minInvestment", "runningTime", "runningTime en J",
    "roi (fourni par Binance)", "roi (calculé)", "pnl", "matchedTrades",
    "Trades / J", "mdd7d", "gridProfit", "gridMode", "gridCount",
    "qtyPerOrderEstimate", "gridProfitEstimateLow", "gridProfitEstimateMid",
    "gridProfitEstimateHigh", "totalProfit", "gridProfitTotalProfitRatio",
    "floatingProfit", "currentPrice", "priceRange", "profitPerGridAfterFees",
    "score",
]


def main(src, dst):
    df = pd.read_csv(src, dtype={"strategyId": str})
    if df.empty:
        raise SystemExit(f"Source vide : {src}")
    missing = [c for c in ("strategyId", "leverage", "symbol") if c not in df]
    if missing:
        raise SystemExit(f"Colonnes indispensables absentes : {missing}")

    df = df[df["strategyId"].notna() & df["symbol"].notna()].drop_duplicates("strategyId").copy()

    def col(name):
        return pd.to_numeric(df[name], errors="coerce") if name in df else pd.Series(np.nan, index=df.index)

    lev, runtime, roi, pnl = col("leverage"), col("runningTime"), col("roi"), col("pnl")
    min_inv, matched, mdd, price = col("minInvestment"), col("matchedTrades"), col("mdd7d"), col("currentPrice")
    days = (runtime / 86400).where(runtime >= 0)
    trades_day = (matched / days).where(days > 0)
    total = col("totalProfit").where(col("totalProfit").notna(), pnl)

    # roi (calculé) = PNL / investissement minimum ; neutralisé s'il s'écarte trop du ROI Binance
    # (le signe peut être négatif : on compare des rapports, pas des valeurs absolues).
    calc = (pnl / min_inv * 100).where(min_inv > 0)
    roi_calc = calc.where((calc / roi.where(roi != 0)).between(ROI_RATIO_MIN, ROI_RATIO_MAX))

    # Grid Profit exact (Binance) ou estimation (Low / Mid / High).
    source = df["gridProfitSource"].fillna("") if "gridProfitSource" in df else pd.Series("", index=df.index)
    exact = source.eq("BINANCE_EXACT") & col("gridProfit").notna()
    gp_exact = col("gridProfit").where(exact)
    low, mid, high, qty = (col(c) for c in ("gridProfitEstimateLow", "gridProfitEstimateMid",
                                            "gridProfitEstimateHigh", "qtyPerOrderEstimate"))
    # Seul un intervalle mal ordonné est neutralisé (pas la ligne entière).
    interval_ok = low.notna() & mid.notna() & high.notna() & (low <= mid + 1e-8) & (mid <= high + 1e-8)
    for s in (low, mid, high, qty):
        s[~(exact | interval_ok)] = np.nan

    # Ratio : exact si Grid Profit exact, sinon ratio central estimé (marqué ≈). Pas de plafond.
    nonzero = total.where(total != 0)
    ratio_exact = (gp_exact / nonzero).where(exact)
    ratio_est = (mid / nonzero).where(~exact & interval_ok)
    ratio = ratio_exact.where(exact, ratio_est)
    ratio_txt = [
        np.nan if pd.isna(v) else (float(f"{v:.6f}") if exact.loc[i] else f"≈{v:.6f}")
        for i, v in ratio.items()
    ]
    floating = (total - gp_exact).where(exact & total.notna())

    out = pd.DataFrame({
        "rank": np.nan, "strategyId": df.strategyId, "category": df.get("category"),
        "strategyType": df.get("strategyType"), "symbol": df.symbol, "leverage": lev,
        "minInvestment": min_inv, "runningTime": runtime, "runningTime en J": days,
        "roi (fourni par Binance)": roi, "roi (calculé)": roi_calc, "pnl": pnl, "matchedTrades": matched,
        "Trades / J": trades_day, "mdd7d": mdd, "gridProfit": gp_exact, "gridMode": df.get("gridMode"),
        "gridCount": col("gridCount"), "qtyPerOrderEstimate": qty, "gridProfitEstimateLow": low,
        "gridProfitEstimateMid": mid, "gridProfitEstimateHigh": high, "totalProfit": total,
        "gridProfitTotalProfitRatio": ratio_txt, "floatingProfit": floating, "currentPrice": price,
        "priceRange": df.get("priceRange"), "profitPerGridAfterFees": df.get("profitPerGridAfterFees"),
        "score": np.nan,
    })[COLUMNS]
    # Tri numérique stable sur strategyId (et non alphabétique : "10" > "9").
    out = out.assign(_k=pd.to_numeric(out.strategyId, errors="coerce")) \
             .sort_values(["_k", "strategyId"], kind="stable").drop(columns="_k").reset_index(drop=True)

    folder = os.path.dirname(dst) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = dst + ".tmp"
    out.to_csv(tmp, index=False)
    os.replace(tmp, dst)

    with open(os.path.join(folder, "current_multicriteria_summary.txt"), "w", encoding="utf-8") as h:
        h.write(f"COUNT={len(out)}\n"
                "UNIVERSE=Binance public Bot Marketplace scan result, leverage<=1; verify results/scan_meta.json "
                "collection_complete before treating the scan as exhaustive; no age/price-range/profit-per-grid filters\n"
                "RANKING=NONE (rank and score columns kept empty)\n"
                "RATIO=exact numeric when Grid Profit is exact; estimated ratio prefixed with ≈; "
                "empty when Total Profit is zero; no 100%/500% cap\n")
    print(f"{len(out)} stratégies -> {dst} | Grid Profit exact : {int(exact.sum())} | "
          f"ratio estimé : {int(ratio_est.notna().sum())} | ratio indéfini : {int(ratio.isna().sum())}")


if __name__ == "__main__":
    s = sys.argv[1] if len(sys.argv) > 1 else "results/current_multicriteria.csv"
    main(s, sys.argv[2] if len(sys.argv) > 2 else s)
