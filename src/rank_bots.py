#!/usr/bin/env python3
"""src/rank_bots.py -- sélection / tri indicatif des Spot Grid (lecture seule : ne modifie pas le CSV).

Priorité 1 = performance issue de la grille (ROI Binance x part Grid Profit, ramené à 30 jours) ;
priorité 2 = risque (mdd7d, prix proche du milieu de la plage). Usage : python src/rank_bots.py [csv] [capital]
"""
import sys

import pandas as pd

MIN_DAYS, MIN_TRADES, MAX_MDD7D, MAX_LEVEL, MIN_PPG, MIN_RATIO, MAX_RATIO = 30, 100, 0.10, 30.0, 0.3, 0.5, 1.0


def rank(df):
    d = df.copy()
    d["days"], d["roi"] = d["runningTime en J"], d["roi (fourni par Binance)"]
    d["gridShare"] = d["gridProfitTotalProfitRatio"].clip(0, 1)
    d["gridRoi30d"] = d["roi"] * d["gridShare"] / d["days"] * 30       # % / mois, extrapolation linéaire
    d["totalRoi30d"] = d["roi"] / d["days"] * 30
    ok = ((d.days >= MIN_DAYS) & (d.matchedTrades >= MIN_TRADES) & (d.roi > 0)
          & (d.currentPrice > d.priceRangeLow) & (d.currentPrice < d.priceRangeHigh) & (d.Niveau.abs() <= MAX_LEVEL)
          & (d.mdd7d <= MAX_MDD7D) & (d.profitPerGridAfterFeesMid >= MIN_PPG)
          & d.gridProfitTotalProfitRatio.between(MIN_RATIO, MAX_RATIO))   # >100 % : estimation peu crédible
    d = d[ok].copy()
    d["band"] = (d.gridRoi30d // 1.0)                                  # paliers de 1 %/mois : la perf prime
    d = d.sort_values(["band", "mdd7d", "Niveau"], key=lambda s: s.abs() if s.name == "Niveau" else s,
                      ascending=[False, True, True])
    return d.drop(columns="band")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "results/current_multicriteria.csv"
    capital = float(sys.argv[2]) if len(sys.argv) > 2 else 100.0
    top = rank(pd.read_csv(path, dtype={"strategyId": str})).head(10)
    top["gain_1_mois"] = capital * top.gridRoi30d / 100
    cols = ["strategyId", "symbol", "days", "roi", "gridProfitTotalProfitRatio", "gridRoi30d", "mdd7d", "Niveau", "gain_1_mois"]
    print(top[cols].round(3).to_string(index=False))
