#!/usr/bin/env python3
"""src/history_ranking.py -- classement des bots par performance observée dans results/history/ (risque ignoré).

  python src/history_ranking.py [dossier_historique=results/history] [sortie=results/history_ranking.csv]
Pour chaque bot : ROI au premier et au dernier instantané où il apparaît, ROI maximal, gain en points de ROI,
gain par jour et ROI moyen par jour depuis le lancement. Classement par gain décroissant.
Fichier séparé : current_multicriteria.csv (format figé) n'est jamais modifié.
Un instantané toutes les STEP_HOURS heures est lu (plus le premier et le dernier) pour garder un temps de calcul court.
Le ROI inclut la variation de prix des jetons, pas seulement le profit de grille.
"""
import datetime as dt
import gzip
import sys
from pathlib import Path

import pandas as pd

STEP_HOURS = 6
MIN_DAYS, MIN_TRADES, MIN_SPAN = 14, 100, 5      # fiabilité : vie >= 14 j, >= 100 trades, observé >= 5 j
TOP = 200
COLS = ["strategyId", "symbol", "minInvestment", "runningTime", "roi", "matchedTrades", "mdd7d"]


def snapshots(history):
    found = []
    for path in Path(history).rglob("*.csv.gz"):
        try:
            found.append((dt.datetime.strptime(path.name[:15], "%Y%m%dT%H%M%S"), path))
        except ValueError:
            continue
    found.sort()
    if not found:
        return []
    kept, last = [found[0]], found[0][0]
    for stamp, path in found[1:-1]:
        if (stamp - last).total_seconds() >= STEP_HOURS * 3600:
            kept.append((stamp, path))
            last = stamp
    if len(found) > 1:
        kept.append(found[-1])
    return kept


def main(history="results/history", out="results/history_ranking.csv", top=TOP):
    snaps = snapshots(history)
    if not snaps:
        print(f"aucun instantané dans {history} -> rien à classer")
        return None
    frames = []
    for stamp, path in snaps:
        try:
            with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
                frame = pd.read_csv(handle, usecols=lambda c: c in COLS, dtype={"strategyId": str})
        except Exception:
            continue
        frames.append(frame.assign(ts=stamp))
    data = pd.concat(frames, ignore_index=True)
    for col in ("minInvestment", "runningTime", "roi", "matchedTrades", "mdd7d"):
        data[col] = pd.to_numeric(data[col], errors="coerce")
    data = data.dropna(subset=["strategyId", "roi", "runningTime"]).sort_values(["strategyId", "ts"])
    grouped = data.groupby("strategyId")
    first, last = grouped.first(), grouped.last()
    latest = data.ts.max()
    res = pd.DataFrame({
        "symbol": last.symbol, "minInvestment": last.minInvestment, "days": last.runningTime / 86400,
        "observed_days": (last.ts - first.ts).dt.total_seconds() / 86400,
        "roi_start": first.roi, "roi_end": last.roi, "roi_peak": grouped.roi.max(),
        "trades": last.matchedTrades, "mdd7d": last.mdd7d, "active": last.ts == latest,
    })
    res["gain_pts"] = res.roi_end - res.roi_start
    res["gain_per_day"] = res.gain_pts / res.observed_days.where(res.observed_days > 0)
    res["roi_per_day_lifetime"] = res.roi_end / res.days.where(res.days > 0)
    total_span = (latest - data.ts.min()).total_seconds() / 86400
    min_span = min(MIN_SPAN, total_span * 0.5)        # historique encore court : seuil réduit
    ok = (res.days >= MIN_DAYS) & (res.trades >= MIN_TRADES) & (res.observed_days >= min_span) & (res.roi_start >= 0)
    res = res[ok].sort_values("gain_pts", ascending=False).head(top)
    res.insert(0, "rank", range(1, len(res) + 1))
    res = res.reset_index().round(4)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    res.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"{len(snaps)} instantanés lus ({data.ts.min():%d/%m %H:%M} -> {latest:%d/%m %H:%M}), "
          f"{data.strategyId.nunique()} bots, {len(res)} classés -> {out}")
    return res


if __name__ == "__main__":
    a = sys.argv
    main(a[1] if len(a) > 1 else "results/history", a[2] if len(a) > 2 else "results/history_ranking.csv")
