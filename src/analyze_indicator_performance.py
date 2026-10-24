#!/usr/bin/env python3
"""Quels indicateurs prédisent réellement la performance des bots ? (walk-forward sur nos historiques)

Pour chaque instant t (un instantané toutes les 24 h, en remontant depuis le plus récent) :
  - indicateurs connus à t seulement (prix Binance klines 1 h si joignables, sinon prix du projet) ;
  - performance réelle du bot sur les 24 h suivantes, lue dans l'instantané suivant :
    variation de PNL / investissement minimum, trades appariés, sortie de fourchette. (La disparition d'un bot n'est pas analysée : la taille des
    scans varie de 6 900 à 15 000 bots d'une heure à l'autre, une absence ne prouve pas un arrêt.)
Sorties :
  results/indicator_performance.csv : pour chaque indicateur x cible, IC de Spearman par tranche de
      temps (moyenne, t-stat sur les tranches), médianes par tiers bas / milieu / haut.
  results/regime_performance.csv    : performance par régime (RANGE / MIXED / TREND) à t.
Les résultats mûrissent avec l'historique : lire `nSlices` et `signal` avant de conclure.
"""
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

import build_bot_indicators as bi

RESULTS = Path("results")
HISTORY = RESULTS / "history"
OUT_IND = RESULTS / "indicator_performance.csv"
OUT_REG = RESULTS / "regime_performance.csv"

HORIZON_H = 24
TOL_H = 1.5            # écart maximal entre l'instant visé et l'instantané choisi
MAX_BOTS_PER_SYMBOL = 10
MIN_SLICE_OBS = 30
MIN_SLICES = 10        # en dessous : signal = INSUFFICIENT
CALM_MOVE_PCT = 2.0    # prix quasi stable sur 24 h -> perf dégagée de la dérive du token
SEED = 7

FEATURES = ["adx14", "chop14", "efficiencyRatio48", "trendScore", "dailyVolPct", "atrPct",
            "zScore72h", "rangePos72hPct", "rsi14", "rangePositionPct", "gridStepPct",
            "volOverStep", "exitRisk96hPct", "gridCount", "runningDays", "mdd7d"]
BINARY_TARGETS = {"exitedRange"}   # tiers : taux (moyenne) au lieu de la médiane
TARGETS = ["pnlPct24h", "pnlPct24hCalm", "trades24h", "exitedRange"]


def num(s):
    return pd.to_numeric(s, errors="coerce")


# ---------------------------------------------------------------- instantanés
def snapshot_index():
    rows = []
    for path in HISTORY.rglob("*.csv.gz"):
        m = re.search(r"(\d{8}T\d{6}Z)", path.name)
        if m:
            rows.append((pd.to_datetime(m.group(1), format="%Y%m%dT%H%M%SZ"), path))
    return sorted(rows)


def nearest(index, target, tol_h=TOL_H):
    best = min(index, key=lambda r: abs((r[0] - target).total_seconds()))
    return best if abs((best[0] - target).total_seconds()) <= tol_h * 3600 else None


def read_snapshot(path):
    try:
        d = pd.read_csv(path, usecols=lambda c: c in {
            "strategyId", "symbol", "minInvestment", "runningTime", "pnl", "matchedTrades",
            "mdd7d", "gridMode", "gridCount", "currentPrice", "priceRange"})
    except Exception:
        return None
    need = {"strategyId", "symbol", "minInvestment", "pnl", "matchedTrades", "currentPrice", "priceRange"}
    if not need.issubset(d.columns):
        return None
    for c in ["strategyId", "minInvestment", "pnl", "matchedTrades", "currentPrice",
              "runningTime", "mdd7d", "gridCount"]:
        if c in d:
            d[c] = num(d[c])
    for c in ["runningTime", "mdd7d", "gridCount", "gridMode"]:
        if c not in d:
            d[c] = np.nan
    rng = d["priceRange"].astype(str).str.extractall(r"([0-9]*\.?[0-9]+(?:[eE][-+]?\d+)?)")[0].unstack()
    for name, pos in (("lo", 0), ("hi", 1)):
        d[name] = num(rng[pos]).reindex(d.index) if pos in rng.columns else np.nan
    d["symbol"] = d["symbol"].astype(str).str.strip()
    d["runningDays"] = d["runningTime"] / 86400.0   # runningTime en secondes
    return d.dropna(subset=["strategyId"]).drop_duplicates("strategyId")


# ---------------------------------------------------------------- indicateurs au fil du temps
def indicator_frame(f, with_chop):
    c = f["close"]
    atr, adx, chop = bi.adx_chop(f)
    ret = np.log(c).diff()
    roll = c.rolling(72)
    out = pd.DataFrame({
        "adx14": adx,
        "chop14": chop if with_chop else np.nan,
        "efficiencyRatio48": (c - c.shift(48)).abs() / c.diff().abs().rolling(48).sum(),
        "dailyVolPct": 100 * ret.rolling(72).std() * math.sqrt(24),
        "atrPct": 100 * atr / c,
        "zScore72h": (c - roll.mean()) / roll.std(),
        "rangePos72hPct": 100 * (c - roll.min()) / (roll.max() - roll.min()),
        "rsi14": bi.rsi(c),
    })
    return out.replace([np.inf, -np.inf], np.nan)


def load_indicators(symbols):
    """(dict symbole -> DataFrame d'indicateurs 1 h, source)."""
    frames, source = {}, "PROJECT_HISTORY"
    if os.environ.get("BINANCE_KLINES", "auto").lower() != "off":
        if bi.fetch_klines("BTCUSDT", "1h") is not None:
            t0 = time.time()

            def job(sym):
                return sym, (bi.fetch_klines(sym, "1h") if time.time() - t0 < bi.KLINE_BUDGET_S else None)
            with ThreadPoolExecutor(max_workers=8) as ex:
                frames = {s: f for s, f in ex.map(job, symbols) if f is not None}
            source = "BINANCE_KLINES"
    project = None
    out = {}
    for sym in symbols:
        if sym in frames:
            out[sym] = indicator_frame(frames[sym], with_chop=True)
        else:
            if project is None:
                project = bi.load_project_history()
            pf = bi.project_frame(project.get(sym), "1h")
            if pf is not None:
                out[sym] = indicator_frame(pf, with_chop=False)
    return out, (source if frames else "PROJECT_HISTORY"), len(frames)


def last_row(f, t0, cols, max_age_h=2.5):
    """Dernière ligne connue à t0 (jamais postérieure), vide si trop ancienne."""
    empty = pd.Series(np.nan, index=cols, dtype=float)
    if f is None or len(f) == 0:
        return empty
    i = f.index.searchsorted(t0, side="right") - 1
    if i < 0 or (t0 - f.index[i]).total_seconds() > max_age_h * 3600:
        return empty
    return f.iloc[i][cols].astype(float)


# ---------------------------------------------------------------- une tranche de temps
def build_slice(t0, s0, s1, ind, rng):
    s0 = s0.sample(frac=1, random_state=int(rng.integers(1_000_000))).groupby("symbol").head(MAX_BOTS_PER_SYMBOL)
    m = s0.merge(s1[["strategyId", "pnl", "matchedTrades", "currentPrice"]].rename(
        columns={"pnl": "pnl1", "matchedTrades": "mt1", "currentPrice": "px1"}),
        on="strategyId", how="left")
    ok = m["minInvestment"] > 0
    m["pnlPct24h"] = np.where(ok, 100 * (m["pnl1"] - m["pnl"]) / m["minInvestment"], np.nan)
    dt = (m["mt1"] - m["matchedTrades"])
    m["trades24h"] = dt.where(dt >= 0)
    move = 100 * (m["px1"] / m["currentPrice"] - 1)
    m["pnlPct24hCalm"] = m["pnlPct24h"].where(move.abs() <= CALM_MOVE_PCT)
    m["exitedRange"] = np.where(m["px1"].isna() | m["lo"].isna(), np.nan,
                                ((m["px1"] < m["lo"]) | (m["px1"] > m["hi"])).astype(float))

    cols = ["adx14", "chop14", "efficiencyRatio48", "dailyVolPct", "atrPct", "zScore72h",
            "rangePos72hPct", "rsi14"]
    sym_rows = {}
    for sym in m["symbol"].unique():
        f = ind.get(sym)
        sym_rows[sym] = last_row(f, t0, cols)
    feat = pd.DataFrame(sym_rows).T
    table = feat.assign(indicatorTimeframe="1h")
    ok_sym = table[["adx14", "efficiencyRatio48"]].notna().all(axis=1)
    label = pd.Series("UNKNOWN", index=table.index)
    score = pd.Series(np.nan, index=table.index)
    if ok_sym.sum() >= 9:
        lab, sc = bi.relative_regimes(table[ok_sym].reset_index(drop=True))
        label[ok_sym] = lab.to_numpy()
        score[ok_sym] = sc.to_numpy()
    feat["regime"], feat["trendScore"] = label, score
    m = m.join(feat, on="symbol")

    mode = m["gridMode"].astype(str).str.upper()
    m["gridStepPct"] = np.where(
        (m["lo"] > 0) & (m["hi"] > m["lo"]) & (m["gridCount"] > 0) & (m["currentPrice"] > 0),
        np.where(mode.eq("GEOMETRIC"), 100 * ((m["hi"] / m["lo"]) ** (1 / m["gridCount"]) - 1),
                 100 * ((m["hi"] - m["lo"]) / m["gridCount"]) / m["currentPrice"]), np.nan)
    m["rangePositionPct"] = np.where(m["hi"] > m["lo"], 100 * (m["currentPrice"] - m["lo"]) / (m["hi"] - m["lo"]), np.nan)
    m["volOverStep"] = m["dailyVolPct"] / m["gridStepPct"].where(m["gridStepPct"] > 0)
    m["exitRisk96hPct"] = [bi.exit_risk(p, lo, hi, v / 100) if pd.notna(v) else np.nan
                           for p, lo, hi, v in zip(m["currentPrice"], m["lo"], m["hi"], m["dailyVolPct"])]
    m["slice"] = t0
    return m


# ---------------------------------------------------------------- agrégation
def spearman(x, y):
    d = pd.concat([x, y], axis=1).dropna()
    if len(d) < MIN_SLICE_OBS or d.iloc[:, 0].nunique() < 3 or d.iloc[:, 1].nunique() < 2:
        return np.nan
    return d.iloc[:, 0].rank().corr(d.iloc[:, 1].rank())


def tercile_buckets(x):
    r = x.rank(pct=True)
    return pd.Series(np.where(r.isna(), "", np.where(r <= 1 / 3, "low", np.where(r > 2 / 3, "high", "mid"))), index=x.index)


def signal_label(n_slices, t):
    if n_slices < MIN_SLICES or pd.isna(t):
        return "INSUFFICIENT"
    return "SIGNIFICANT" if abs(t) >= 2 else "NOT_SIGNIFICANT"


def indicator_table(obs, source):
    rows = []
    slices = sorted(obs["slice"].unique())
    bucket = {f: obs.groupby("slice")[f].transform(tercile_buckets) for f in FEATURES}
    for f in FEATURES:
        for t in TARGETS:
            ics = [spearman(g[f], g[t]) for _, g in obs.groupby("slice")]
            ics = [v for v in ics if pd.notna(v)]
            k = len(ics)
            mean = float(np.mean(ics)) if k else np.nan
            sd = float(np.std(ics, ddof=1)) if k > 1 else np.nan
            tstat = mean / (sd / math.sqrt(k)) if k > 1 and sd and sd > 0 else np.nan
            grouped = obs.assign(b=bucket[f]).groupby("b")[t]
            med = grouped.mean() if t in BINARY_TARGETS else grouped.median()
            low, mid, high = (med.get(b, np.nan) for b in ("low", "mid", "high"))
            rows.append({
                "feature": f, "target": t, "nSlices": k,
                "nObs": int(obs[[f, t]].dropna().shape[0]),
                "icMean": mean, "icT": tstat,
                "lowTercileMedian": low, "midTercileMedian": mid, "highTercileMedian": high,
                "highMinusLow": high - low if pd.notna(low) and pd.notna(high) else np.nan,
                "signal": signal_label(k, tstat), "source": source,
                "firstSlice": slices[0].strftime("%Y-%m-%dT%H:%MZ"),
                "lastSlice": slices[-1].strftime("%Y-%m-%dT%H:%MZ"),
            })
    out = pd.DataFrame(rows)
    out["absT"] = out["icT"].abs()
    out = out.sort_values(["target", "absT"], ascending=[True, False]).drop(columns="absT")
    return out.round(4)


def tstat(values):
    v = pd.Series(values).dropna()
    if len(v) < 3 or v.std(ddof=1) == 0:
        return np.nan
    return float(v.mean() / (v.std(ddof=1) / math.sqrt(len(v))))


def regime_table(obs):
    """Une ligne par régime + la ligne RANGE_minus_TREND (écart de PNL médian par tranche, t-stat)."""
    rows = []
    per_slice = obs.groupby(["slice", "regime"])["pnlPct24h"].median().unstack()
    for reg, g in obs.groupby("regime"):
        pnl = g["pnlPct24h"].dropna()
        rows.append({
            "regime": reg, "nObs": len(g), "nSymbols": g["symbol"].nunique(),
            "nSlices": g["slice"].nunique(),
            "medianPnlPct24h": pnl.median(),
            "meanPnlPct24hClipped": pnl.clip(pnl.quantile(0.01), pnl.quantile(0.99)).mean() if len(pnl) else np.nan,
            "winRate": (pnl > 0).mean() if len(pnl) else np.nan,
            "medianPnlPct24hCalm": g["pnlPct24hCalm"].median(),
            "medianTrades24h": g["trades24h"].median(),
            "exitedRangeRate": g["exitedRange"].mean(),
            "tStatVsZero": tstat(per_slice[reg]) if reg in per_slice else np.nan,
        })
    if {"RANGE", "TREND"}.issubset(per_slice.columns):
        diff = (per_slice["RANGE"] - per_slice["TREND"]).dropna()
        rows.append({"regime": "RANGE_minus_TREND", "nSlices": len(diff),
                     "medianPnlPct24h": diff.mean(), "tStatVsZero": tstat(diff)})
    return pd.DataFrame(rows).round(4)


def main():
    index = snapshot_index()
    if len(index) < 4:
        raise SystemExit("Historique insuffisant")
    latest = index[-1][0]
    rng = np.random.default_rng(SEED)

    pairs, k = [], 1
    while True:
        t0 = latest - pd.Timedelta(hours=HORIZON_H * k)
        a, b = nearest(index, t0), nearest(index, t0 + pd.Timedelta(hours=HORIZON_H))
        if t0 < index[0][0] - pd.Timedelta(hours=TOL_H):
            break
        k += 1
        if a and b and a[1] != b[1]:
            pairs.append((a, b))
    loaded = []
    for a, b in pairs:
        s0, s1 = read_snapshot(a[1]), read_snapshot(b[1])
        if s0 is not None and s1 is not None and s0["lo"].notna().any():
            loaded.append((a[0], s0, s1))
    if len(loaded) < 2:
        raise SystemExit("Pas assez de tranches exploitables")

    symbols = sorted(set().union(*[set(s0["symbol"]) for _, s0, _ in loaded]))
    ind, source, n_klines = load_indicators(symbols)
    print(f"{len(loaded)} tranches ; indicateurs {source} ({len(ind)}/{len(symbols)} paires)")

    obs = pd.concat([build_slice(t0, s0, s1, ind, rng) for t0, s0, s1 in loaded], ignore_index=True)
    table = indicator_table(obs, source)
    reg = regime_table(obs)
    for frame, path in ((table, OUT_IND), (reg, OUT_REG)):
        tmp = path.with_suffix(".csv.tmp")
        frame.to_csv(tmp, index=False)
        tmp.replace(path)
    print(f"{len(obs)} observations ; {OUT_IND} ({len(table)} lignes), {OUT_REG}")


if __name__ == "__main__":
    main()
