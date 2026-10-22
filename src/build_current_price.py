#!/usr/bin/env python3
"""Build a pair-level price table and preserve observed extrema across project snapshots."""
from pathlib import Path
import gzip
import csv
import pandas as pd

RESULTS = Path("results")
CURRENT = RESULTS / "current_multicriteria.csv"
TRACKER = RESULTS / "current_price_history.csv"
OUTPUT = RESULTS / "current_price.csv"
HISTORY = RESULTS / "history"
OUT_COLUMNS = ["symbol", "minInvestment", "currentPrice", "priceLow", "pruceHigh", "Niveau"]


def numeric(series):
    return pd.to_numeric(series, errors="coerce")


def read_snapshot(path):
    try:
        frame = pd.read_csv(path, compression="gzip", usecols=lambda c: c in {"symbol", "currentPrice"})
        if {"symbol", "currentPrice"}.issubset(frame.columns):
            frame["symbol"] = frame["symbol"].astype(str).str.strip()
            frame["currentPrice"] = numeric(frame["currentPrice"])
            return frame[["symbol", "currentPrice"]].dropna()
    except Exception:
        pass
    return pd.DataFrame(columns=["symbol", "currentPrice"])


def main():
    current = pd.read_csv(CURRENT, dtype={"symbol": str})
    required = {"symbol", "minInvestment", "currentPrice", "priceRangeLow", "priceRangeHigh", "Niveau"}
    missing = sorted(required - set(current.columns))
    if missing:
        raise SystemExit(f"Colonnes absentes de {CURRENT}: {missing}")

    current["symbol"] = current["symbol"].astype(str).str.strip()
    current["minInvestment"] = numeric(current["minInvestment"])
    current["currentPrice"] = numeric(current["currentPrice"])
    current["Niveau"] = numeric(current["Niveau"])
    current = current[current["symbol"].ne("") & current["symbol"].notna()].copy()
    if current.empty:
        raise SystemExit("Aucune paire exploitable dans current_multicriteria.csv")

    # On backfill l'historique du projet une seule fois, puis on met à jour le registre à chaque scan.
    if TRACKER.exists():
        history = pd.read_csv(TRACKER, dtype={"symbol": str})
    else:
        frames = []
        for path in HISTORY.rglob("*.csv.gz"):
            frame = read_snapshot(path)
            if not frame.empty:
                frames.append(frame)
        history = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["symbol", "currentPrice"])

    history["symbol"] = history.get("symbol", pd.Series(dtype=str)).astype(str).str.strip()
    history["currentPrice"] = numeric(history.get("currentPrice", pd.Series(dtype=float)))
    history = history[history["symbol"].ne("") & history["currentPrice"].gt(0)]
    now = current[["symbol", "currentPrice"]].dropna()
    now = now[now["currentPrice"].gt(0)]
    observations = pd.concat([history[["symbol", "currentPrice"]], now], ignore_index=True)
    extrema = observations.groupby("symbol", as_index=False).agg(
        priceLow=("currentPrice", "min"), priceHigh=("currentPrice", "max")
    ).rename(columns={"priceHigh": "pruceHigh"})
    # Le registre conserve les extrêmes historiques même si une paire disparaît temporairement du scan courant.
    TRACKER.parent.mkdir(parents=True, exist_ok=True)
    extrema.to_csv(TRACKER, index=False)

    # Une ligne par paire. Le Niveau n'est pas une propriété intrinsèque de la paire :
    # on retient celui du bot dont le minInvestment est le plus faible pour cette paire.
    current = current.sort_values(["symbol", "minInvestment"], ascending=[True, True], na_position="last", kind="stable")
    pair_rows = current.drop_duplicates("symbol", keep="first")[
        ["symbol", "minInvestment", "currentPrice", "Niveau"]
    ]
    result = pair_rows.merge(extrema, on="symbol", how="left")
    result = result[OUT_COLUMNS].sort_values("symbol", kind="stable")
    temp = OUTPUT.with_suffix(".csv.tmp")
    result.to_csv(temp, index=False)
    temp.replace(OUTPUT)
    print(f"{len(result)} paires -> {OUTPUT}; extrema calculés depuis les archives disponibles + le scan courant.")


if __name__ == "__main__":
    main()
