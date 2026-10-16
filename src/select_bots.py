#!/usr/bin/env python3
"""src/select_bots.py -- présélection indicative des bots (performance d'abord, risque ensuite).

Lit results/current_multicriteria.csv (format figé, jamais modifié) et écrit results/shortlist.csv.
  python src/select_bots.py [csv_final] [max_par_actif=1]
Si results/history/ contient des instantanés vieux de 1 à 10 jours (référence choisie bot par bot, cible 7 j), la performance et l'activité RÉCENTES
(variation du ROI et de matchedTrades) servent de filtre « bot vivant » et plafonnent le rendement ;
sinon ces contrôles sont ignorés (colonnes vides) et le script le signale.
Tout est exprimé en pourcentage du capital engagé (aucun montant en USDT) ; `minInvestment` indique la mise minimale du bot.
Les performances passées ne garantissent pas les performances futures : projection indicative, pas une promesse.
"""
import datetime as dt
import gzip
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

FEE = 0.001
MATURITY_DAYS = 15           # coefficient de fiabilité : jours / (jours + 15)
RECENT_TARGET_DAYS = 7       # fenêtre de comparaison visée pour la performance récente
RECENT_MIN_DAYS = 1.0        # écart minimal entre deux instantanés pour mesurer quelque chose
QUOTES = ("FDUSD", "USDT", "USDC")

# (libellé, condition(d)) -- appliquées dans l'ordre ; le nombre de bots restants est affiché.
GATES = [
    ("cotation stable USDT/USDC/FDUSD", lambda d: d.symbol.str.endswith(QUOTES)),
    ("durée >= 14 j", lambda d: d.days >= 14),
    ("matchedTrades >= 100 et Trades/J >= 3", lambda d: (d.matchedTrades >= 100) & (d.trades_day >= 3)),
    ("ROI Binance >= 1 % (estimations non bruitées)", lambda d: d.roi >= 1),
    ("prix dans la plage, |Niveau| <= 40", lambda d: d.Niveau.abs() <= 40),
    ("mdd7d <= 10 %", lambda d: d.mdd7d <= 0.10),
    ("floating >= -10 % du capital", lambda d: d.floating_pct >= -10),
    ("profit/grille après frais >= 0,3 %", lambda d: d.ppg >= 0.3),
    ("ratio Grid/Total estimé entre 0,6 et 1,5", lambda d: d.ratio.between(0.6, 1.5)),
    ("rendement 30 j / perte au bas de plage >= 0,3", lambda d: d.month_pct / d.stress_loss_pct >= 0.3),
    ("bot vivant (7 j : ROI > 0 et >= 1 trade/j) si historique", lambda d: d.alive),
]


def levels(r):
    lo, hi, n = r.priceRangeLow, r.priceRangeHigh, int(r.gridCount)
    if str(r.gridMode).upper() == "GEOMETRIC":
        return lo * (hi / lo) ** (np.arange(n + 1) / n)
    return lo + (hi - lo) / n * np.arange(n + 1)


def stress_loss_pct(r):
    """Perte latente (% du capital) si le prix descend jusqu'au bas de la plage, sans gains de grille."""
    try:
        lv, p = levels(r), r.currentPrice
        qty = 1.0 / ((lv[lv < p].sum() + (lv > p).sum() * p) * (1 + FEE))
        loss = qty * ((lv > p).sum() * (p - lv[0]) + np.clip(lv[lv < p] - lv[0], 0, None).sum())
        return loss * 100                                # capital normalisé à 1 : le % ne dépend pas de la mise
    except Exception:
        return np.nan


def base_asset(symbol):
    s = str(symbol)
    return next((s[:-len(q)] for q in QUOTES if s.endswith(q) and len(s) > len(q)), s)


def load_reference(history, now=None):
    """Référence récente PAR BOT : parmi les instantanés vieux de 1 à 10 jours (un par demi-journée), on retient
    pour chaque bot celui où il figure et dont l'âge est le plus proche de 7 jours. Un scan partiel (collecte
    incomplète) ne prive donc plus un bot de sa mesure récente. Retourne un DataFrame indexé par strategyId
    (colonnes roi, matchedTrades, age) ou None."""
    now = now or dt.datetime.now(dt.timezone.utc)
    buckets = {}
    for path in Path(history).rglob("*.csv.gz"):
        try:
            stamp = dt.datetime.strptime(path.name[:15], "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        age = (now - stamp).total_seconds() / 86400
        if RECENT_MIN_DAYS <= age <= 10:
            key = int(age * 2)                       # un instantané par demi-journée, le plus proche du milieu
            if key not in buckets or abs(age - (key + 0.5) / 2) < abs(buckets[key][0] - (key + 0.5) / 2):
                buckets[key] = (age, path)
    frames = []
    for age, path in buckets.values():
        try:
            with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
                snap = pd.read_csv(handle, dtype={"strategyId": str}, usecols=["strategyId", "roi", "matchedTrades"])
        except Exception:
            continue
        snap = snap.drop_duplicates("strategyId").assign(age=age)
        frames.append(snap)
    if not frames:
        return None
    allsnaps = pd.concat(frames)
    allsnaps["gap"] = (allsnaps.age - RECENT_TARGET_DAYS).abs()
    return allsnaps.sort_values("gap").drop_duplicates("strategyId").set_index("strategyId")


def main(path="results/current_multicriteria.csv", per_asset=1, now=None):
    d = pd.read_csv(path, dtype={"strategyId": str})
    d = d.rename(columns={"roi (fourni par Binance)": "roi", "runningTime en J": "days", "Trades / J": "trades_day",
                          "gridProfitTotalProfitRatio": "ratio", "profitPerGridAfterFeesMid": "ppg"})
    capital_implicit = d.pnl / (d.roi / 100)                       # capital déduit du ROI Binance (arrondi)
    d["floating_pct"] = d.floatingProfit / capital_implicit.where(d.roi > 0) * 100
    # Rendement journalier prudent depuis le lancement : min(ROI total, ROI de grille borne basse) / durée.
    grid_low_roi = d.roi * d.gridProfitEstimateLow / d.totalProfit.where(d.totalProfit != 0)
    d["daily_pct"] = np.minimum(d.roi, grid_low_roi) / d.days

    # Performance et activité récentes (historique horaire), si disponibles.
    ref = load_reference(Path(path).parent / "history", now)
    d["recent_age_days"], d["recent_daily_pct"], d["recent_trades_day"], d["alive"] = np.nan, np.nan, np.nan, True
    if ref is not None:
        age = pd.to_numeric(d.strategyId.map(ref["age"]), errors="coerce")
        roi_then = pd.to_numeric(d.strategyId.map(ref["roi"]), errors="coerce")
        trades_then = pd.to_numeric(d.strategyId.map(ref["matchedTrades"]), errors="coerce")
        d["recent_age_days"] = age
        d["recent_daily_pct"] = (d.roi - roi_then) / age
        d["recent_trades_day"] = (d.matchedTrades - trades_then) / age
        known = d.recent_daily_pct.notna()
        d["daily_pct"] = np.where(known, np.minimum(d.daily_pct, d.recent_daily_pct), d.daily_pct)
        d["alive"] = ~known | ((d.recent_daily_pct > 0) & (d.recent_trades_day >= 1))
        print(f"historique : {int(known.sum())}/{len(d)} bots avec mesure récente (âge médian {age.median():.1f} j) ; "
              f"{int((~known).sum())} sans référence -> acceptés sans contrôle récent")
    else:
        print("historique : aucun instantané de 1 à 10 jours -> filtre « bot vivant » et rendement récent ignorés")

    d["maturity"] = d.days / (d.days + MATURITY_DAYS)
    d["score"] = d.daily_pct * d.maturity                          # critère de tri ; daily_pct reste interprétable
    d["month_pct"] = d.daily_pct * 30
    d["stress_loss_pct"] = d.apply(stress_loss_pct, axis=1)

    print(f"{'départ':58s}{len(d):6d}")
    for label, cond in GATES:
        d = d[cond(d)]
        print(f"{label:58s}{len(d):6d}")
    d["base"] = d.symbol.map(base_asset)
    d = d.sort_values(["score", "mdd7d"], ascending=[False, True])
    d = d.groupby("base", sort=False).head(per_asset)              # diversification : <= N bots par actif de base
    d["reward_stress"] = d.month_pct / d.stress_loss_pct
    d.insert(0, "rank", range(1, len(d) + 1))
    cols = ["rank", "strategyId", "symbol", "minInvestment", "days", "roi", "ratio", "trades_day",
            "recent_trades_day", "recent_age_days", "daily_pct", "recent_daily_pct", "maturity", "score", "month_pct",
            "mdd7d", "Niveau", "ppg", "stress_loss_pct", "reward_stress"]
    out = d[cols].round(4)
    dst = Path(path).parent / "shortlist.csv"
    out.to_csv(dst, index=False, encoding="utf-8-sig")             # BOM : les tableurs lisent correctement 币安人生
    print(f"-> {dst} ({len(out)} bots)")
    return out


if __name__ == "__main__":
    a = sys.argv
    main(a[1] if len(a) > 1 else "results/current_multicriteria.csv", int(a[2]) if len(a) > 2 else 1)
