#!/usr/bin/env python3
"""Classement de TOUS les bots -> results/bot_ranking.csv (tri uniquement : aucune ligne n'est retirée).

(src/rank_bots.py reste votre sélection filtrée ; ce script ne filtre rien.)

Ordre du classement final (rankGridFavorability, 1 = le plus favorable au grid) :
  1. FAVORABILITÉ AU GRID (critère principal) : le marché et le réglage du bot conviennent-ils à un grid ?
     gridFavorabilityScore (0-100) = 50 % régime de marché (range plutôt que tendance, retour à la moyenne)
     + 25 % prix centré dans la plage + 25 % économie de la grille (profit par grille après frais).
     Les bots sont répartis en 5 niveaux de taille égale (gridFavorabilityLabel).
  2. à l'intérieur d'un niveau : probabilité de réussite à 96 h décroissante ;
  3. puis rentabilité attendue décroissante (% du capital, hors risque).
Pour un tri sur la favorabilité seule, trier sur gridFavorabilityScore. rankProfitability et rankProbability
donnent les classements globaux de chaque axe.

Entrées : results/current_multicriteria.csv et results/bot_indicators.csv (jointure sur strategyId).
"""
from pathlib import Path

import numpy as np
import pandas as pd

RESULTS = Path("results")
CURRENT = RESULTS / "current_multicriteria.csv"
INDICATORS = RESULTS / "bot_indicators.csv"
OUTPUT = RESULTS / "bot_ranking.csv"

HORIZON_DAYS = 4          # horizon de la probabilité de réussite (96 h)
# Cycles complets (achat + vente) par jour ~ A * (volOverStep^2 / 2)^B.
# Constantes ajustées le 25/10/2026 sur les bots actifs depuis >= 30 jours (corrélation log-log 0,87) :
# le modèle brut (A=1, B=1) surestimait de 25 %, et davantage pour les bots très actifs.
CYCLES_A, CYCLES_B = 0.81, 0.87
# Poids de la favorabilité au grid (choix explicite, modifiable ; la somme doit faire 1).
W_REGIME, W_CENTER, W_ECON = 0.50, 0.25, 0.25
HURST_BEST, HURST_WORST = 0.40, 0.60   # Hurst <= 0,40 : retour à la moyenne marqué ; >= 0,60 : persistance
PPG_FLOOR, PPG_GOOD = 0.3, 0.5         # profit par grille après frais (%) : critère du dépôt (> 0,3 %, idéal >= 0,5 %)
LABELS = [(0.2, "TRES_FAVORABLE"), (0.4, "FAVORABLE"), (0.6, "NEUTRE"), (0.8, "PEU_FAVORABLE"), (1.0, "DEFAVORABLE")]
TIER_HIGH, TIER_MID = 0.80, 0.50   # probabilité >= 80 % : ELEVEE ; >= 50 % : MOYENNE ; sinon FAIBLE


def num(s):
    return pd.to_numeric(s, errors="coerce")


def score(df):
    """Ajoute les colonnes de score à `df`. Ne retire aucune ligne."""
    d = df.copy()
    in_range = (d["currentPrice"] > d["priceRangeLow"]) & (d["currentPrice"] < d["priceRangeHigh"])
    d["inRange"] = in_range

    # --- axe 1 : rentabilité attendue, hors risque ---------------------------------------------
    cycles = CYCLES_A * (d["volOverStep"].clip(lower=0) ** 2 / 2) ** CYCLES_B
    d["estCyclesPerDay"] = cycles
    ppg = d["profitPerGridAfterFeesPct"]
    # Un cycle rapporte `ppg` % sur la mise d'une grille, soit 1/gridCount du capital.
    ret = cycles * ppg / d["gridCount"]
    d["expectedGridReturnPctPerDay"] = ret.where(in_range, 0.0).where(d["volOverStep"].notna())
    d["expectedGridReturnPct30d"] = d["expectedGridReturnPctPerDay"] * 30

    # --- axe 2 : probabilité de réussite à 96 h ---------------------------------------------------
    p_stay = (1 - d["exitRisk96hPct"] / 100).where(in_range, 0.0)
    p_cycle = pd.Series(np.where(ppg > 0, 1 - np.exp(-cycles * HORIZON_DAYS), 0.0),
                        index=d.index).where(cycles.notna())
    d["pStayInRange96hPct"] = 100 * p_stay
    d["pCycle96hPct"] = 100 * p_cycle
    d["pSuccess96hPct"] = 100 * p_stay * p_cycle
    p = d["pSuccess96hPct"] / 100
    d["probabilityTier"] = np.where(p.isna(), "", np.where(p >= TIER_HIGH, "ELEVEE",
                                                           np.where(p >= TIER_MID, "MOYENNE", "FAIBLE")))

    # --- rangs (1 = meilleur ; valeurs manquantes en dernier, jamais retirées) ------------------
    order = d.sort_values(["expectedGridReturnPctPerDay", "pSuccess96hPct", "strategyId"],
                          ascending=[False, False, True], na_position="last").index
    d["rankProfitability"] = pd.Series(range(1, len(d) + 1), index=order).reindex(d.index)
    order = d.sort_values(["pSuccess96hPct", "expectedGridReturnPctPerDay", "strategyId"],
                          ascending=[False, False, True], na_position="last").index
    d["rankProbability"] = pd.Series(range(1, len(d) + 1), index=order).reindex(d.index)
    # --- favorabilité au grid (critère principal) ---------------------------------------------------
    trend = d["trendScore"]
    hurst_score = 100 * ((HURST_WORST - d["hurst"]) / (HURST_WORST - HURST_BEST)).clip(0, 1)
    regime = (0.7 * (100 - trend) + 0.3 * hurst_score).where(hurst_score.notna(), 100 - trend)
    d["regimeScore"] = regime                       # 100 = marché type range, 0 = forte tendance
    lo, hi, px = d["priceRangeLow"], d["priceRangeHigh"], d["currentPrice"]
    with np.errstate(divide="ignore", invalid="ignore"):
        pos_lin = (px - lo) / (hi - lo)
        pos_log = np.log(px / lo) / np.log(hi / lo)  # une grille géométrique se lit en échelle logarithmique
    pos = pd.Series(np.where(d["gridMode"].astype(str).str.upper().eq("GEOMETRIC"), pos_log, pos_lin), index=d.index)
    d["centralityScore"] = (100 * (1 - (2 * pos - 1).abs())).where(in_range, 0.0)   # 100 = au milieu
    econ = np.select([ppg <= 0, ppg < PPG_FLOOR, ppg < PPG_GOOD],
                     [0.0, 50 * ppg / PPG_FLOOR, 50 + 50 * (ppg - PPG_FLOOR) / (PPG_GOOD - PPG_FLOOR)], 100.0)
    d["economicsScore"] = pd.Series(econ, index=d.index).where(ppg.notna())
    d["gridFavorabilityScore"] = (W_REGIME * d["regimeScore"] + W_CENTER * d["centralityScore"]
                                  + W_ECON * d["economicsScore"])

    scored = d["gridFavorabilityScore"].notna()
    by_score = d["gridFavorabilityScore"].rank(ascending=False, method="min")   # ex æquo : même niveau
    share = by_score / max(int(scored.sum()), 1)
    tier = pd.Series(0, index=d.index)
    label = pd.Series("", index=d.index)
    for i, (upper, name) in reversed(list(enumerate(LABELS, start=1))):
        hit = scored & (share <= upper)
        tier = tier.where(~hit, i)
        label = label.where(~hit, name)
    d["gridFavorabilityLabel"] = label
    d["_tier"] = tier.where(scored, len(LABELS) + 1)  # sans donnée : après le dernier niveau, conservé

    # Ordre final : niveau de favorabilité, puis probabilité, puis rentabilité.
    order = d.sort_values(["_tier", "pSuccess96hPct", "expectedGridReturnPctPerDay", "strategyId"],
                          ascending=[True, False, False, True], na_position="last").index
    d["rankGridFavorability"] = pd.Series(range(1, len(d) + 1), index=order).reindex(d.index)
    d = d.drop(columns="_tier")

    valid = d["expectedGridReturnPctPerDay"].notna()
    quint = np.ceil(d["rankProfitability"] / max(int(valid.sum()), 1) * 5).clip(1, 5)
    d["profitabilityQuintile"] = quint.where(valid)   # 1 = 20 % les plus rentables, 5 = 20 % les moins
    return d


OUT_COLUMNS = [
    "rankGridFavorability", "gridFavorabilityLabel", "gridFavorabilityScore",
    "regimeScore", "centralityScore", "economicsScore",
    "rankProfitability", "rankProbability", "strategyId", "symbol", "gridMode", "gridCount",
    "currentPrice", "priceRangeLow", "priceRangeHigh", "inRange", "rangePositionPct",
    "gridStepPct", "profitPerGridAfterFeesPct", "volOverStep",
    "estCyclesPerDay", "observedTradesPerDay",
    "expectedGridReturnPctPerDay", "expectedGridReturnPct30d", "profitabilityQuintile",
    "realizedGridRoi30dPct", "runningDays",
    "pStayInRange96hPct", "pCycle96hPct", "pSuccess96hPct", "probabilityTier",
]


def main():
    cur = pd.read_csv(CURRENT)
    ind = pd.read_csv(INDICATORS)
    keep = ["strategyId", "gridStepPct", "volOverStep", "exitRisk96hPct", "rangePositionPct", "trendScore", "hurst"]
    d = cur.merge(ind[keep], on="strategyId", how="left")
    for c in ["gridCount", "currentPrice", "priceRangeLow", "priceRangeHigh", "profitPerGridAfterFeesMid",
              "Trades / J", "runningTime en J", "roi (fourni par Binance)", "gridProfitTotalProfitRatio"]:
        d[c] = num(d[c])
    d["profitPerGridAfterFeesPct"] = d["profitPerGridAfterFeesMid"]
    d["observedTradesPerDay"] = d["Trades / J"]
    d["runningDays"] = d["runningTime en J"]
    # Rendement réalisé, même définition que src/rank_bots.py : ROI Binance x part Grid Profit / jours x 30.
    d["realizedGridRoi30dPct"] = (
        d["roi (fourni par Binance)"] * d["gridProfitTotalProfitRatio"].clip(0, 1) / d["runningDays"] * 30
    ).where(d["runningDays"] > 0)

    out = score(d)
    if len(out) != len(cur):
        raise SystemExit("une ligne a été perdue : le classement ne doit rien retirer")
    out = out.sort_values("rankGridFavorability")[OUT_COLUMNS]
    for c in out.columns:
        if out[c].dtype.kind == "f":
            out[c] = out[c].round(4)
    tmp = OUTPUT.with_suffix(".csv.tmp")
    out.to_csv(tmp, index=False)
    tmp.replace(OUTPUT)
    print(f"{len(out)} bots classés -> {OUTPUT}")


if __name__ == "__main__":
    main()
