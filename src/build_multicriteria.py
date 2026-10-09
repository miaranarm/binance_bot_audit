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

FIABILITÉ ET EXHAUSTIVITÉ
  - ne jamais filtrer un bot en fonction de l'âge, du prix hors plage ou du profit/grille
  - le ratio est calculé si le total est non nul, sans plafond à 100 % ni à 500 %
  - le ratio exact vient du Grid Profit Binance exact ; sinon le ratio central estimé
    est affiché avec le préfixe ≈, et Grid Profit reste vide pour signaler l'estimation
  - conserver les estimations négatives si les données sources le permettent
  - ne jamais supprimer une ligne à cause d'une donnée financière incohérente ; neutraliser
    uniquement le calcul concerné et conserver les données brutes utiles

SCORE 0-100 (indicatif, ne constitue pas un conseil d'investissement)
  Ratio profit de grille / profit total ............. 20   min(ratio, 1)
  Rendement de grille annualisé ajusté du risque ..... 15   percentile de (profit de grille annualisé / mdd7d)
  Prix au milieu de la plage ......................... 20   100 % au centre, 0 aux bornes, 0 hors plage
  Ancienneté ......................................... 15   palier 365 jours
  Nombre de trades ................................... 10   palier 300 trades
  Profit par grille après frais ...................... 10   0 sous 0,3 % ; plein tarif de 0,5 à 1,5 % ; 80 % au-delà
  Activité (trades / jour) ........................... 10   percentile
  Éligibilité : Grid Profit exact, ratio calculable, Profit/Grid > 0,3 %, ROI/MDD/durée valides. Prix hors plage -> score 0.
"""
import os
import sys

import numpy as np
import pandas as pd

MAX_CAPITAL_USD = 1_000_000
ROI_RATIO_MIN, ROI_RATIO_MAX = 0.5, 10
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

    # Dédupliquer uniquement les identifiants, puis conserver tout bot du périmètre
    # déjà collecté. Aucun filtrage sur l'âge, la plage de prix ou le profit/grille.
    df = df.drop_duplicates("strategyId")
    lev = pd.to_numeric(df.leverage, errors="coerce")
    df = df[df["strategyId"].notna() & df["symbol"].notna()].copy()
    lev = pd.to_numeric(df.leverage, errors="coerce")

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
    exact = source.eq("BINANCE_EXACT") & grid_raw.notna()
    gp_exact = grid_raw.where(exact)

    low, mid, high, qty = (col("gridProfitEstimateLow"), col("gridProfitEstimateMid"),
                           col("gridProfitEstimateHigh"), col("qtyPerOrderEstimate"))
    # Les valeurs restent visibles même si elles semblent atypiques. On ne neutralise
    # que l'intervalle mal ordonné, pas la ligne entière ni les autres métriques.
    interval_ok = low.notna() & mid.notna() & high.notna() & (low <= mid + 1e-8) & (mid <= high + 1e-8)
    for s in (low, mid, high, qty):
        s[~(exact | interval_ok)] = np.nan

    # Ratio exact si Grid Profit exact ; sinon estimation centrale marquée ≈.
    # Pas de plafond à 1 (100 %) ou 5 (500 %). Un total nul rend le ratio indéfini.
    ratio_exact = (gp_exact / total.where(total != 0)).where(exact)
    ratio_est = (mid / total.where(total != 0)).where((~exact) & interval_ok)
    ratio = ratio_exact.copy()
    ratio.loc[ratio_est.notna()] = ratio_est.loc[ratio_est.notna()]

    # Floating Profit n'est dérivé que lorsque Grid Profit est exact et que les
    # deux valeurs sont sur la même base comptable Binance.
    floating = (total - gp_exact).where(exact & total.notna())

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
    valid = pd.Series(False, index=df.index)  # classement désactivé pendant la fiabilisation

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

    # Le score et le classement sont volontairement neutralisés jusqu'à ce que
    # les métriques prioritaires soient fiabilisées. Ils ne doivent pas masquer
    # ni réordonner l'univers complet des bots.
    score = pd.Series(np.nan, index=df.index, dtype="float64")

    out = pd.DataFrame({
        "strategyId": df.strategyId, "category": df.get("category"), "strategyType": df.get("strategyType"),
        "symbol": df.symbol, "leverage": lev[df.index], "minInvestment": min_inv, "runningTime": runtime,
        "runningTime en J": days, "roi (fourni par Binance)": roi, "roi (calculé)": roi_calc, "pnl": pnl,
        "matchedTrades": matched, "Trades / J": trades_day, "mdd7d": mdd, "gridProfit": gp_exact,
        "gridMode": df.get("gridMode"), "gridCount": col("gridCount"), "qtyPerOrderEstimate": qty,
        "gridProfitEstimateLow": low, "gridProfitEstimateMid": mid, "gridProfitEstimateHigh": high,
        "totalProfit": total,
        "gridProfitTotalProfitRatio": [
            (float(f"{v:.6f}") if exact.loc[i] else f"≈{v:.6f}") if pd.notna(v) else np.nan
            for i, v in ratio.items()
        ],
        "floatingProfit": floating,
        "currentPrice": price, "priceRange": df.get("priceRange"),
        "profitPerGridAfterFees": df.get("profitPerGridAfterFees"), "score": score,
    })
    # Ordre stable de la liste source : aucune présélection ni classement actif.
    out = out.sort_values("strategyId", key=lambda s: s.astype(str), kind="stable").reset_index(drop=True)
    scored = out["score"].notna()
    out.insert(0, "rank", pd.Series(np.nan, index=out.index, dtype="float64"))
    out = out[COLUMNS]

    folder = os.path.dirname(dst) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = dst + ".tmp"
    out.to_csv(tmp, index=False)
    os.replace(tmp, dst)

    with open(os.path.join(folder, "current_multicriteria_summary.txt"), "w", encoding="utf-8") as h:
        h.write(f"COUNT={len(out)}\\nUNIVERSE=Binance public Bot Marketplace; complete collected universe; no age/price-range/profit-per-grid filters\\n")
        h.write("SCORING=DISABLED_DURING_DATA_QUALITY_PHASE\\n")
        h.write(f"RATIO=exact numeric when Grid Profit is exact; estimated ratio prefixed with ≈ when Grid Profit is estimated; no arbitrary 100%/500% cap\\n")

    print(f"{len(out)} stratégies conservées -> {dst}")
    print(f"Grid Profit exact : {int(exact.sum())} | ratio estimé disponible : {int(ratio_est.notna().sum())} "
          f"| ratio indéfini : {int(ratio.isna().sum())} | score désactivé : {len(out)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "results/current_multicriteria.csv",
         sys.argv[2] if len(sys.argv) > 2 else (sys.argv[1] if len(sys.argv) > 1 else "results/current_multicriteria.csv"))
