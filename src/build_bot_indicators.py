#!/usr/bin/env python3
"""Indicateurs adaptés à chaque bot -> results/bot_indicators.csv (une ligne par strategyId).

Source des prix, par paire :
  1. klines publiques Binance (OHLC, 1000 barres) si joignables  -> dataSource = BINANCE_KLINES
  2. à défaut, prix horaires de results/history/ (clôtures seules) -> dataSource = PROJECT_HISTORY
Variable d'environnement BINANCE_KLINES=off pour forcer l'historique du projet.

Étiquettes uniquement : aucune ligne n'est retirée, aucun bot n'est écarté.
"""
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

RESULTS = Path("results")
CURRENT = RESULTS / "current_multicriteria.csv"
HISTORY = RESULTS / "history"
OUTPUT = RESULTS / "bot_indicators.csv"

KLINE_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]
KLINE_BUDGET_S = 600
MIN_BARS = 60
HORIZON_H = 96  # horizon du risque de sortie de fourchette
TF_HOURS = {"15m": 0.25, "1h": 1.0, "4h": 4.0}

OUT_COLUMNS = [
    "strategyId", "symbol", "gridMode", "gridCount", "priceRangeLow", "priceRangeHigh",
    "currentPrice", "rangePositionPct", "gridStepPct",
    "indicatorTimeframe", "dataSource", "nBars", "historyDays",
    "dailyVolPct", "atrPct", "volOverStep",
    "adx14", "chop14", "hurst", "efficiencyRatio48", "regime",
    "rsi14", "zScore72h", "rangePos72hPct",
    "distToLowATR", "distToHighATR", "exitRisk96hPct",
    "bestIndicators",
]


def num(s):
    return pd.to_numeric(s, errors="coerce")


# ---------------------------------------------------------------- données de prix
def wanted_timeframe(trades_per_day):
    if pd.notna(trades_per_day) and trades_per_day >= 48:
        return "15m"
    if pd.notna(trades_per_day) and trades_per_day >= 8:
        return "1h"
    return "4h"


def fetch_klines(symbol, tf, session=None):
    """OHLC depuis les klines publiques ; None si indisponible."""
    import requests
    session = session or requests
    for host in KLINE_HOSTS:
        try:
            r = session.get(f"{host}/api/v3/klines",
                            params={"symbol": symbol, "interval": tf, "limit": 1000}, timeout=15)
            if r.status_code != 200:
                continue
            frame = parse_klines(r.json())
            if frame is not None:
                return frame
        except Exception:
            continue
    return None


def parse_klines(rows):
    if not isinstance(rows, list) or len(rows) < MIN_BARS:
        return None
    f = pd.DataFrame(rows).iloc[:, :6]
    f.columns = ["t", "open", "high", "low", "close", "volume"]
    for c in ["open", "high", "low", "close", "volume"]:
        f[c] = num(f[c])
    f["t"] = pd.to_datetime(f["t"], unit="ms")
    f = f.dropna(subset=["high", "low", "close"]).set_index("t")
    f = f.iloc[:-1]  # la dernière barre est en cours : on l'écarte
    return f[["high", "low", "close"]] if len(f) >= MIN_BARS else None


def load_project_history():
    """Clôtures horaires par paire depuis results/history (colonne 'currentPrice' ou 'price')."""
    frames = []
    for path in sorted(HISTORY.rglob("*.csv.gz")):
        m = re.search(r"(\d{8}T\d{6}Z)", path.name)
        if not m:
            continue
        try:
            d = pd.read_csv(path, usecols=lambda c: c in {"symbol", "currentPrice", "price"})
        except Exception:
            continue
        col = "currentPrice" if "currentPrice" in d else ("price" if "price" in d else None)
        if col is None or "symbol" not in d:
            continue
        d = d.rename(columns={col: "p"})[["symbol", "p"]].drop_duplicates("symbol")
        d["p"] = num(d["p"])
        d["t"] = pd.to_datetime(m.group(1), format="%Y%m%dT%H%M%SZ")
        frames.append(d.dropna())
    if not frames:
        return {}
    h = pd.concat(frames, ignore_index=True)
    h = h[h["p"] > 0]
    out = {}
    for symbol, g in h.groupby("symbol"):
        s = g.set_index("t")["p"].sort_index().resample("1h").last().dropna()
        out[symbol] = s
    return out


def project_frame(series, tf):
    """Clôtures seules : high = low = close (le TR devient |Δclôture|)."""
    if series is None or len(series) < MIN_BARS:
        return None
    if tf == "4h":
        series = series.resample("4h").last().dropna()
    if len(series) < MIN_BARS:
        return None
    return pd.DataFrame({"high": series, "low": series, "close": series})


# ---------------------------------------------------------------- indicateurs
def wilder(s, n):
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def adx_chop(f, n=14):
    h, l, c = f["high"], f["low"], f["close"]
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    up, dn = h.diff(), -l.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr = wilder(tr, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * wilder(pd.Series(pdm, index=f.index), n) / atr
        mdi = 100 * wilder(pd.Series(mdm, index=f.index), n) / atr
        dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
    adx = wilder(dx.fillna(0), n)
    span = h.rolling(n).max() - l.rolling(n).min()
    with np.errstate(divide="ignore", invalid="ignore"):
        chop = 100 * np.log10(tr.rolling(n).sum() / span) / math.log10(n)
    return atr, adx, chop


def hurst(close, max_lag=20):
    x = np.log(close.dropna().to_numpy()[-300:])
    if len(x) < 60:
        return np.nan
    lags = range(2, max_lag)
    sd = [np.std(x[lag:] - x[:-lag]) for lag in lags]
    if min(sd) <= 0:
        return np.nan
    return float(np.polyfit(np.log(list(lags)), np.log(sd), 1)[0])


def rsi(close, n=14):
    d = close.diff()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = wilder(d.clip(lower=0), n) / wilder((-d).clip(lower=0), n)
    return 100 - 100 / (1 + rs)


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def symbol_indicators(f, tf):
    """Indicateurs d'une paire sur sa fréquence ; dernières valeurs."""
    c = f["close"]
    bars_h = 1 / TF_HOURS[tf]
    atr, adx, chop = adx_chop(f)
    ret = np.log(c).diff().dropna()
    daily_vol = float(ret.std() * math.sqrt(24 * bars_h))
    n72 = max(10, int(round(72 * bars_h)))
    w = c.iloc[-n72:]
    sd = w.std()
    n48 = min(48, len(c) - 1)
    er = abs(c.iloc[-1] - c.iloc[-1 - n48]) / c.diff().abs().iloc[-n48:].sum()
    return {
        "dailyVolPct": 100 * daily_vol,
        "atrPct": 100 * float(atr.iloc[-1] / c.iloc[-1]),
        "atr": float(atr.iloc[-1]),
        "adx14": float(adx.iloc[-1]),
        "chop14": float(chop.iloc[-1]),
        "hurst": hurst(c),
        "efficiencyRatio48": float(er),
        "rsi14": float(rsi(c).iloc[-1]),
        "zScore72h": float((c.iloc[-1] - w.mean()) / sd) if sd > 0 else np.nan,
        "rangePos72hPct": float(100 * (c.iloc[-1] - w.min()) / (w.max() - w.min()))
        if w.max() > w.min() else np.nan,
        "nBars": int(len(c)),
        "historyDays": float((f.index[-1] - f.index[0]).total_seconds() / 86400),
        "lastClose": float(c.iloc[-1]),
    }


def regime(adx, er, chop=None):
    """Étiquette de régime (jamais un filtre). CHOP (seuils 38,2 / 61,8) seulement avec de vraies
    bougies OHLC ; sur clôtures seules il est biaisé et n'est pas utilisé. Hurst reste informatif."""
    if pd.isna(adx) or pd.isna(er):
        return "UNKNOWN"
    trend = int(adx >= 25) + int(er >= 0.30)
    rng = int(adx < 20) + int(er <= 0.15)
    if chop is not None and pd.notna(chop):
        trend += int(chop < 38.2)
        rng += int(chop > 61.8)
    need = 2
    if trend >= need and trend > rng:
        return "TREND"
    if rng >= need and rng > trend:
        return "RANGE"
    return "MIXED"


def grid_step_pct(row):
    lo, hi, n, px = row["priceRangeLow"], row["priceRangeHigh"], row["gridCount"], row["currentPrice"]
    if not (lo > 0 and hi > lo and n > 0 and px > 0):
        return np.nan
    if str(row["gridMode"]).upper() == "GEOMETRIC":
        return 100 * ((hi / lo) ** (1 / n) - 1)
    return 100 * ((hi - lo) / n) / px


def exit_risk(px, lo, hi, daily_vol):
    """P(toucher une borne en 96 h), mouvement brownien sans dérive sur le log-prix (hypothèse)."""
    if not (px > 0 and lo > 0 and hi > px > lo and daily_vol and daily_vol > 0):
        return np.nan
    s = daily_vol * math.sqrt(HORIZON_H / 24)
    p_low = 2 * (1 - norm_cdf(math.log(px / lo) / s))
    p_high = 2 * (1 - norm_cdf(math.log(hi / px) / s))
    return 100 * min(1.0, p_low + p_high)


def best_indicators(r):
    """Les 3 indicateurs les plus pertinents pour ce bot (règles, non validées statistiquement)."""
    picks = []
    pos = r["rangePositionPct"]
    if pd.notna(pos) and (pos < 15 or pos > 85):
        picks += ["distToLowATR" if pos < 15 else "distToHighATR", "exitRisk96hPct"]
    if pd.notna(r["volOverStep"]) and r["volOverStep"] < 0.5:
        picks.append("volOverStep")
    if r["indicatorTimeframe"] == "15m":
        picks += ["chop14", "adx14"]
    else:
        picks += ["adx14", "chop14"]
    picks += ["zScore72h", "hurst"]
    seen = []
    for p in picks:
        if p not in seen:
            seen.append(p)
    return "|".join(seen[:3])


# ---------------------------------------------------------------- pipeline
def main():
    cur = pd.read_csv(CURRENT, dtype={"symbol": str})
    for c in ["strategyId", "gridCount", "priceRangeLow", "priceRangeHigh", "currentPrice", "Trades / J"]:
        cur[c] = num(cur[c])
    cur["symbol"] = cur["symbol"].astype(str).str.strip()
    cur["tfWanted"] = cur["Trades / J"].map(wanted_timeframe)

    project = load_project_history()
    pairs = sorted(set(zip(cur["symbol"], cur["tfWanted"])))
    klines = {}
    if os.environ.get("BINANCE_KLINES", "auto").lower() != "off":
        t0 = time.time()
        probe = fetch_klines("BTCUSDT", "1h")
        if probe is not None:
            def job(sym_tf):
                return sym_tf, (fetch_klines(*sym_tf) if time.time() - t0 < KLINE_BUDGET_S else None)
            with ThreadPoolExecutor(max_workers=8) as ex:
                klines = {k: v for k, v in ex.map(job, pairs) if v is not None}
    print(f"klines Binance : {len(klines)}/{len(pairs)} (symbole, fréquence)")

    cache = {}
    for sym, tf in pairs:
        if (sym, tf) in klines:
            frame, src, used = klines[(sym, tf)], "BINANCE_KLINES", tf
        else:
            used = "4h" if tf == "4h" else "1h"
            frame, src = project_frame(project.get(sym), used), "PROJECT_HISTORY"
        if frame is None:
            cache[(sym, tf)] = None
            continue
        try:
            ind = symbol_indicators(frame, used)
        except Exception:
            cache[(sym, tf)] = None
            continue
        ind.update(indicatorTimeframe=used, dataSource=src)
        cache[(sym, tf)] = ind

    rows = []
    for _, b in cur.iterrows():
        ind = cache.get((b["symbol"], b["tfWanted"]))
        r = {"strategyId": int(b["strategyId"]) if pd.notna(b["strategyId"]) else "",
             "symbol": b["symbol"], "gridMode": b["gridMode"], "gridCount": b["gridCount"],
             "priceRangeLow": b["priceRangeLow"], "priceRangeHigh": b["priceRangeHigh"],
             "currentPrice": b["currentPrice"]}
        lo, hi, px = b["priceRangeLow"], b["priceRangeHigh"], b["currentPrice"]
        r["rangePositionPct"] = 100 * (px - lo) / (hi - lo) if hi > lo else np.nan
        r["gridStepPct"] = grid_step_pct(b)
        if ind is None:
            r.update(indicatorTimeframe=b["tfWanted"], dataSource="INSUFFICIENT")
        else:
            r.update({k: ind[k] for k in ind if k not in {"atr", "lastClose"}})
            r["volOverStep"] = ind["dailyVolPct"] / r["gridStepPct"] if r["gridStepPct"] > 0 else np.nan
            ohlc = ind["dataSource"] == "BINANCE_KLINES"
            r["regime"] = regime(ind["adx14"], ind["efficiencyRatio48"], ind["chop14"] if ohlc else None)
            if not ohlc:
                r["chop14"] = np.nan  # CHOP sur clôtures seules : biaisé, non publié
            atr_abs = ind["atr"] * (px / ind["lastClose"]) if ind["lastClose"] > 0 else np.nan
            r["distToLowATR"] = (px - lo) / atr_abs if atr_abs > 0 else np.nan
            r["distToHighATR"] = (hi - px) / atr_abs if atr_abs > 0 else np.nan
            r["exitRisk96hPct"] = exit_risk(px, lo, hi, ind["dailyVolPct"] / 100)
        r.setdefault("regime", "UNKNOWN")
        r["bestIndicators"] = best_indicators({
            "rangePositionPct": r["rangePositionPct"], "volOverStep": r.get("volOverStep", np.nan),
            "indicatorTimeframe": r["indicatorTimeframe"]})
        rows.append(r)

    out = pd.DataFrame(rows).reindex(columns=OUT_COLUMNS).sort_values("strategyId", kind="stable")
    for c in out.columns:
        if out[c].dtype.kind == "f":
            out[c] = out[c].round(4)
    tmp = OUTPUT.with_suffix(".csv.tmp")
    out.to_csv(tmp, index=False)
    tmp.replace(OUTPUT)
    print(f"{len(out)} bots -> {OUTPUT} ; sources : {out['dataSource'].value_counts().to_dict()}")


if __name__ == "__main__":
    main()
