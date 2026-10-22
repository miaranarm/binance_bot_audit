#!/usr/bin/env python3
"""Build pair-level current prices and preserve price extrema across archived project snapshots."""
from pathlib import Path
import pandas as pd

RESULTS = Path("results")
CURRENT = RESULTS / "current_multicriteria.csv"
TRACKER = RESULTS / "current_price_history.csv"
OUTPUT = RESULTS / "current_price.csv"
HISTORY = RESULTS / "history"
OUT_COLUMNS = ["symbol", "minInvestment", "currentPrice", "priceLow", "pruceHigh", "Niveau"]


def num(series):
    return pd.to_numeric(series, errors="coerce")


def read_snapshot(path):
    try:
        frame = pd.read_csv(path, compression="gzip", usecols=lambda c: c in {"symbol", "currentPrice"})
        if {"symbol", "currentPrice"}.issubset(frame.columns):
            frame["symbol"] = frame["symbol"].astype(str).str.strip()
            frame["currentPrice"] = num(frame["currentPrice"])
            return frame[["symbol", "currentPrice"]].dropna()
    except Exception:
        pass
    return pd.DataFrame(columns=["symbol", "currentPrice"])


def main():
    current = pd.read_csv(CURRENT, dtype={"symbol": str})
    required = {"symbol", "minInvestment", "currentPrice", "Niveau"}
    missing = sorted(required - set(current.columns))
    if missing:
        raise SystemExit(f"Colonnes absentes de {CURRENT}: {missing}")
    current["symbol"] = current["symbol"].astype(str).str.strip()
    current["minInvestment"] = num(current["minInvestment"])
    current["currentPrice"] = num(current["currentPrice"])
    current["Niveau"] = num(current["Niveau"])
    current = current[current["symbol"].ne("") & current["symbol"].notna()].copy()
    if current.empty:
        raise SystemExit("Aucune paire exploitable dans current_multicriteria.csv")

    now = current[["symbol", "currentPrice"]].dropna()
    now = now[now["currentPrice"].gt(0)]
    if TRACKER.exists():
        # Durable min/max accumulated in previous scheduled runs.
        prior = pd.read_csv(TRACKER, dtype={"symbol": str})
        prior["symbol"] = prior["symbol"].astype(str).str.strip()
        prior["priceLow"] = num(prior["priceLow"])
        prior["pruceHigh"] = num(prior["pruceHigh"])
        current_extrema = now.groupby("symbol", as_index=False).agg(
            observedLow=("currentPrice", "min"), observedHigh=("currentPrice", "max"))
        extrema = prior.merge(current_extrema, on="symbol", how="outer")
        extrema["priceLow"] = pd.concat([extrema["priceLow"], extrema["observedLow"]], axis=1).min(axis=1)
        extrema["pruceHigh"] = pd.concat([extrema["pruceHigh"], extrema["observedHigh"]], axis=1).max(axis=1)
        extrema = extrema[["symbol", "priceLow", "pruceHigh"]]
    else:
        # First run: reconstruct extrema from every retained hourly archive since the project began.
        frames = []
        for path in HISTORY.rglob("*.csv.gz"):
            frame = read_snapshot(path)
            if not frame.empty:
                frames.append(frame)
        if frames:
            observations = pd.concat(frames + [now], ignore_index=True)
        else:
            observations = now
        extrema = observations.groupby("symbol", as_index=False).agg(
            priceLow=("currentPrice", "min"), pruceHigh=("currentPrice", "max"))

    RESULTS.mkdir(parents=True, exist_ok=True)
    extrema.to_csv(TRACKER, index=False)

    # One row per pair; Niveau depends on each bot's own grid range, so use the bot
    # requiring the lowest minInvestment as a documented pair-level convention.
    current = current.sort_values(["symbol", "minInvestment"], ascending=[True, True],
                                  na_position="last", kind="stable")
    pair_rows = current.drop_duplicates("symbol", keep="first")[
        ["symbol", "minInvestment", "currentPrice", "Niveau"]]
    result = pair_rows.merge(extrema, on="symbol", how="left")[OUT_COLUMNS].sort_values("symbol", kind="stable")
    temp = OUTPUT.with_suffix(".csv.tmp")
    result.to_csv(temp, index=False)
    temp.replace(OUTPUT)
    print(f"{len(result)} paires -> {OUTPUT}; extrema reconstruits depuis l'historique archivé puis actualisés à chaque scan.")


if __name__ == "__main__":
    main()
