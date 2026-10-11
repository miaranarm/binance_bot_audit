#!/usr/bin/env python3
"""src/select_bots.py -- présélection indicative des bots (performance d'abord, risque ensuite).

Lit results/current_multicriteria.csv (format figé, jamais modifié) et écrit results/shortlist.csv.
  python src/select_bots.py [csv_final] [capital_USDT=100] [max_par_symbole=1]
Les performances passées ne garantissent pas les performances futures : projection indicative, pas une promesse.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

FEE = 0.001
GATES = [   # (libellé, condition) -- chaque porte est appliquée dans l'ordre, le compte restant est affiché
    ("cotation stable USDT/USDC/FDUSD", lambda d, cap: d.symbol.str.endswith(("USDT", "USDC", "FDUSD"))),
    ("minInvestment <= capital", lambda d, cap: d.minInvestment <= cap),
    ("durée >= 14 j", lambda d, cap: d.days >= 14),
    ("matchedTrades >= 100 et Trades/J >= 1", lambda d, cap: (d.matchedTrades >= 100) & (d.trades_day >= 1)),
    ("ROI Binance >= 1 % (estimations non bruitées)", lambda d, cap: d.roi >= 1),
    ("prix dans la plage, |Niveau| <= 40", lambda d, cap: d.Niveau.abs() <= 40),
    ("mdd7d <= 8 %", lambda d, cap: d.mdd7d <= 0.08),
    ("floating >= -10 % du capital", lambda d, cap: d.floating_pct >= -10),
    ("profit/grille après frais >= 0,3 %", lambda d, cap: d.ppg >= 0.3),
    ("ratio Grid/Total estimé entre 0,5 et 1,5", lambda d, cap: d.ratio.between(0.5, 1.5)),
]


def levels(r):
    lo, hi, n = r.priceRangeLow, r.priceRangeHigh, int(r.gridCount)
    if str(r.gridMode).upper() == "GEOMETRIC":
        q = (hi / lo) ** (1 / n)
        return lo * q ** np.arange(n + 1)
    return lo + (hi - lo) / n * np.arange(n + 1)


def stress_loss_pct(r, capital):
    """Perte latente (% du capital) si le prix descend jusqu'au bas de la plage, sans gains de grille."""
    try:
        lv, p = levels(r), r.currentPrice
        qty = capital / ((lv[lv < p].sum() + (lv > p).sum() * p) * (1 + FEE))
        loss = qty * ((lv > p).sum() * (p - lv[0]) + np.clip(lv[lv < p] - lv[0], 0, None).sum())
        return loss / capital * 100
    except Exception:
        return np.nan


def main(path="results/current_multicriteria.csv", capital=100.0, per_symbol=1):
    d = pd.read_csv(path, dtype={"strategyId": str})
    d = d.rename(columns={"roi (fourni par Binance)": "roi", "runningTime en J": "days", "Trades / J": "trades_day",
                          "gridProfitTotalProfitRatio": "ratio", "profitPerGridAfterFeesMid": "ppg"})
    capital_implicit = d.pnl / (d.roi / 100)                       # capital déduit du ROI Binance (arrondi)
    d["floating_pct"] = d.floatingProfit / capital_implicit.where(d.roi > 0) * 100
    # Rendement journalier prudent (% du capital/jour) = min(ROI total, ROI de grille borne basse) / durée :
    # on ne compte ni la hausse du jeton (ratio < 1) ni un bot déjà sous l'eau (ratio > 1).
    grid_low_roi = d.roi * d.gridProfitEstimateLow / d.totalProfit.where(d.totalProfit != 0)
    d["daily_pct"] = np.minimum(d.roi, grid_low_roi) / d.days
    print(f"{'départ':48s}{len(d):6d}")
    for label, cond in GATES:
        d = d[cond(d, capital)]
        print(f"{label:48s}{len(d):6d}")
    d = d.sort_values(["daily_pct", "mdd7d"], ascending=[False, True])
    d = d.groupby("symbol", sort=False).head(per_symbol)           # diversification : <= N bots par symbole
    d["month_pct"] = d.daily_pct * 30
    d["gain_30d_usdt"] = d.month_pct / 100 * capital
    d["stress_loss_pct"] = d.apply(stress_loss_pct, axis=1, capital=capital)
    cols = ["strategyId", "symbol", "minInvestment", "days", "roi", "ratio", "daily_pct", "month_pct",
            "gain_30d_usdt", "mdd7d", "Niveau", "ppg", "stress_loss_pct"]
    out = d[cols].round(4)
    dst = Path(path).parent / "shortlist.csv"
    out.to_csv(dst, index=False)
    print(f"-> {dst} ({len(out)} bots)")
    return out


if __name__ == "__main__":
    a = sys.argv
    main(a[1] if len(a) > 1 else "results/current_multicriteria.csv", float(a[2]) if len(a) > 2 else 100.0,
         int(a[3]) if len(a) > 3 else 1)
