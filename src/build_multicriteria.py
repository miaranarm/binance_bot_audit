#!/usr/bin/env python3
"""
src/build_multicriteria.py  --  étape finale du pipeline (remplace finalize_multicriteria.py)

Lit le CSV brut écrit par src/audit_all.py (results/current_multicriteria.csv, avec
colonnes de provenance) et le RÉÉCRIT en place avec uniquement les 29 colonnes
demandées, un score refait et un rang. Le brut complet (provenance incluse) reste
archivé chaque heure dans results/history/*.csv.gz.

Convention de lecture (aucune colonne de provenance dans le fichier final) :
  - gridProfit rempli   -> valeur EXACTE lue chez Binance (gridProfitSource = BINANCE_EXACT)
  - gridProfit vide     -> estimation uniquement dans les colonnes Low / Mid / High
  - le ratio principal et floatingProfit restent vides si le Grid Profit n'est pas exact.
    Une estimation ne doit jamais être présentée comme un ratio officiel ou un floating profit fiable.

ANOMALIES : une donnée non fiable est mise à vide, jamais inventée
  - estimations négatives ou désordonnées (Low > Mid > High) -> vides
  - capital implicite (pnl / roi) > 1 M USD : pnl suspect -> estimations vides, pas de score
  - roi (calculé) = pnl / minInvestment n'a de sens que si minInvestment ~ capital réel :
    vide si l'écart avec le ROI Binance dépasse un facteur 10 (ou < 0,5)
  - ratio profit de grille / profit total : vide si profit total <= 0 ou ratio hors [0 ; 5]

SCORE 0-100 (indicatif, ne constitue pas un conseil d'investissement)
  Ratio profit de grille / profit total ............. 20   min(ratio, 1)
  Rendement de grille annualisé ajusté du risque ..... 15   percentile de (profit de grille annualisé / mdd7d)
  Prix au milieu de la plage ......................... 20   100 % au centre, 0 aux bornes, 0 hors plage
  Ancienneté ......................................... 15   palier 365 jours
  Nombre de trades ................................... 10   palier 300 trades
  Profit par grille après frais ...................... 10   0 sous 0,3 % ; plein tarif de 0,5 à 1,5 % ; 80 % au-delà
  Activité (trades / jour) ........................... 10   percentile
  Barrages : prix hors plage -> 0 (bot inactif) ; profit par grille <= 0,3 % -> score x 0,5.
"""
import os
import sys

import numpy as np
import pandas as pd

MAX_CAPITAL_USD = 1_000_000
ROI_RATIO_MIN, ROI_RATIO_MAX = 0.5, 10
MAX_GRID_RATIO = 5
MIN_DAYS_ANNUALIZE, MAX_ANNUAL_PCT = 14, 300.0

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


def parse_range(s):
    try:
        a, b = str(s).split(" - ")
        return float(a), float(b)
    except Exception:
        return np.nan, np.nan


def parse_ppg_min(s):
    """'0.7672% - 2.9932%' -> 0.7672 ; '0.2037%' -> 0.2037."""
    try:
        return min(float(x.replace("%", "")) for x in str(s).split(" - "))
    except Exception:
        return np.nan


def main(src, dst):
    df = pd.read_csv(src)
    if df.empty:
        raise SystemExit(f"Source vide : {src}")
    missing = [c for c in ("strategyId", "leverage", "symbol") if c not in df]
    if missing:
        raise SystemExit(f"Colonnes indispensables absentes : {missing}")

    df = df.drop_duplicates("strategyId")
    lev = pd.to_numeric(df.leverage, errors="coerce")
    df = df[lev.notna() & (lev <= 1) & df.symbol.notna()].copy()

    def col(name):
        return pd.to_numeric(df[name], errors="coerce") if name in df else pd.Series(np.nan, index=df.index)

    runtime, roi, pnl = col("runningTime"), col("roi"), col("pnl")
    min_inv, matched, mdd, price = col("minInvestment"), col("matchedTrades"), col("mdd7d"), col("currentPrice")
    days = (runtime / 86400).where(runtime >= 0)
    trades_day = (matched / days).where(days > 0)
    total = col("totalProfit").where(col("totalProfit").notna(), pnl)

    # --- roi (calculé), neutralisé si incohérent avec le ROI Binance
    calc = (pnl / min_inv * 100).where(min_inv > 0)
    factor = calc / roi.where(roi > 0)
    roi_calc = calc.where(factor.between(ROI_RATIO_MIN, ROI_RATIO_MAX))

    # --- profit de grille : exact (Binance) ou estimation (Mid)
    source = df["gridProfitSource"].fillna("") if "gridProfitSource" in df else pd.Series("", index=df.index)
    grid_raw = col("gridProfit")
    exact = source.eq("BINANCE_EXACT") & grid_raw.ge(0)
    gp_exact = grid_raw.where(exact)

    capital = (pnl / (roi / 100)).where((roi > 0) & (pnl > 0))      # capital réel en USD
    suspect = (capital > MAX_CAPITAL_USD) & ~exact

    low, mid, high, qty = (col("gridProfitEstimateLow"), col("gridProfitEstimateMid"),
                           col("gridProfitEstimateHigh"), col("qtyPerOrderEstimate"))
    est_ok = exact | (~suspect & low.notna() & mid.notna() & high.notna()
                      & (low >= 0) & (low <= mid + 1e-8) & (mid <= high + 1e-8))
    for s in (low, mid, high, qty):
        s[~est_ok] = np.nan

    # Le ratio principal est un indicateur central : ne jamais le calculer à
    # partir d'un Grid Profit reconstruit. Les estimations restent visibles dans
    # Low / Mid / High, mais ne sont pas assez fiables pour un ratio sans colonne
    # de provenance dans le tableau final.
    gp_used = gp_exact.where(exact, mid)
    ratio = (gp_exact / total.where(total > 0))
    ratio = ratio.where(ratio.between(0, MAX_GRID_RATIO))

    # Floating Profit n'est dérivé que lorsque Grid Profit est exact et que les
    # deux valeurs sont sur la même base comptable Binance.
    floating = (total - gp_exact).where(exact & ratio.notna())

    # --- score
    lo_hi = df.priceRange.map(parse_range)
    lo = pd.Series([x[0] for x in lo_hi], index=df.index)
    hi = pd.Series([x[1] for x in lo_hi], index=df.index)
    pos = ((price - lo) / (hi - lo)).where(hi > lo)
    ppg_min = df.profitPerGridAfterFees.map(parse_ppg_min) if "profitPerGridAfterFees" in df else pd.Series(np.nan, index=df.index)

    # Un score de sélection n'est crédible que si son critère n°1 est
    # mesuré à partir de valeurs exactes. Ne pas remplacer une donnée absente
    # par zéro, ni utiliser le Grid Profit estimé pour classer les bots.
    # Le seuil Profit/Grid > 0,3 % est une condition d'éligibilité, pas un bonus.
    valid = (
        roi.notna() & mdd.notna() & (days > 0) & ~suspect
        & exact & ratio.notna() & gp_exact.notna()
        & ppg_min.notna() & (ppg_min > 0.3)
    )

    a1 = ratio.clip(0, 1)

    days_c = days.clip(lower=MIN_DAYS_ANNUALIZE)
    grid_annual = (gp_exact / capital / days_c * 365 * 100).clip(upper=MAX_ANNUAL_PCT)
    calmar = grid_annual / (mdd * 100).clip(lower=1)
    a2 = pd.Series(0.0, index=df.index)
    m = valid & calmar.notna()
    a2[m] = calmar[m].rank(pct=True)

    b = (1 - (pos - 0.5).abs() * 2).clip(0, 1).fillna(0)
    c1 = (days / 365).clip(0, 1).fillna(0)
    c2 = (matched / 300).clip(0, 1).fillna(0)
    d = pd.Series(np.select(
        [ppg_min.isna(), ppg_min <= 0.3, ppg_min < 0.5, ppg_min <= 1.5],
        [0.0, 0.0, (ppg_min - 0.3) / 0.2, 1.0], 0.8), index=df.index)
    e = pd.Series(0.0, index=df.index)
    m = valid & trades_day.notna()
    e[m] = trades_day[m].rank(pct=True)

    score = 20 * a1 + 15 * a2 + 20 * b + 15 * c1 + 10 * c2 + 10 * d + 10 * e
    # Les bots à Profit/Grid <= 0,3 % ne sont pas éligibles au score.
    score[(pos < 0) | (pos > 1)] = 0.0                 # hors plage : bot inactif
    score[~valid] = np.nan
    score = score.round(1)

    out = pd.DataFrame({
        "strategyId": df.strategyId, "category": df.get("category"), "strategyType": df.get("strategyType"),
        "symbol": df.symbol, "leverage": lev[df.index], "minInvestment": min_inv, "runningTime": runtime,
        "runningTime en J": days, "roi (fourni par Binance)": roi, "roi (calculé)": roi_calc, "pnl": pnl,
        "matchedTrades": matched, "Trades / J": trades_day, "mdd7d": mdd, "gridProfit": gp_exact,
        "gridMode": df.get("gridMode"), "gridCount": col("gridCount"), "qtyPerOrderEstimate": qty,
        "gridProfitEstimateLow": low, "gridProfitEstimateMid": mid, "gridProfitEstimateHigh": high,
        "totalProfit": total, "gridProfitTotalProfitRatio": ratio, "floatingProfit": floating,
        "currentPrice": price, "priceRange": df.get("priceRange"),
        "profitPerGridAfterFees": df.get("profitPerGridAfterFees"), "score": score,
    })
    out = out.sort_values("score", ascending=False, na_position="last", kind="stable").reset_index(drop=True)
    # Le rang n'existe que si le score est calculable. Un bot sans Grid Profit
    # exact reste dans le CSV pour conserver les données, mais ne reçoit pas
    # artificiellement un rang de classement.
    scored = out["score"].notna()
    ranks = pd.Series(np.nan, index=out.index, dtype="float64")
    ranks.loc[scored] = np.arange(1, int(scored.sum()) + 1)
    out.insert(0, "rank", ranks)
    out = out[COLUMNS]

    folder = os.path.dirname(dst) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = dst + ".tmp"
    out.to_csv(tmp, index=False)
    os.replace(tmp, dst)

    with open(os.path.join(folder, "current_multicriteria_summary.txt"), "w", encoding="utf-8") as h:
        h.write(f"COUNT={len(out)}\\nUNIVERSE=Binance public Bot Marketplace, leverage <= 1, unique strategyId\\n")
        h.write(f"SCORED={int(scored.sum())}\\nUNSCORED={int((~scored).sum())}\\n")
        h.write("SCORING=GRID_TOTAL_RATIO20 GRID_RETURN_PER_RISK15 PRICE_CENTRE20 AGE15 TRADES10 PROFIT_PER_GRID10 TRADES_PER_DAY10 (indicatif)\\n")
        if not scored.any():
            h.write("NO_RANKED_BOTS=aucun bot ne satisfait actuellement les conditions de score, notamment Grid Profit exact et Profit/Grid > 0.3%\\n")
        else:
            for _, r in out.loc[scored].head(20).iterrows():
                h.write(f"{int(r['rank'])}. {r.strategyId} {r.symbol} score={r.score} ratio={r.gridProfitTotalProfitRatio} "
                        f"roi={r['roi (fourni par Binance)']} jours={r['runningTime en J']:.0f} mdd7d={r.mdd7d}\\n")

    print(f"{len(out)} stratégies -> {dst}")
    print(f"gridProfit exact : {int(exact.sum())} | estimé (Mid) : {int((~exact & est_ok & mid.notna()).sum())} "
          f"| sans estimation : {int(gp_used.isna().sum())} | sans score : {int(out.score.isna().sum())}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "results/current_multicriteria.csv",
         sys.argv[2] if len(sys.argv) > 2 else (sys.argv[1] if len(sys.argv) > 1 else "results/current_multicriteria.csv"))
