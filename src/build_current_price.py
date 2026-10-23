#!/usr/bin/env python3
"""Build pair-level prices and project-observed historical extrema."""
from pathlib import Path
import pandas as pd

RESULTS = Path("results")
CURRENT = RESULTS / "current_multicriteria.csv"
PROJECT_TRACKER = RESULTS / "current_price_history.csv"
OUTPUT = RESULTS / "current_price.csv"
HISTORY = RESULTS / "history"
OUT_COLUMNS = ["symbol", "minInvestment", "currentPrice", "priceLow", "priceHigh"]


def num(series):
    return pd.to_numeric(series, errors="coerce")


def read_snapshot(path):
    try:
        frame = pd.read_csv(path, compression="gzip",
                            usecols=lambda column: column in {"symbol", "currentPrice"})
        if {"symbol", "currentPrice"}.issubset(frame.columns):
            frame["symbol"] = frame["symbol"].astype(str).str.strip()
            frame["currentPrice"] = num(frame["currentPrice"])
            return frame[["symbol", "currentPrice"]].dropna()
    except Exception:
        pass
    return pd.DataFrame(columns=["symbol", "currentPrice"])


def main():
    current = pd.read_csv(
        CURRENT, dtype={"symbol": str},
        usecols=lambda column: column in {"symbol", "minInvestment", "currentPrice"},
    )
    required = {"symbol", "minInvestment", "currentPrice"}
    missing = sorted(required - set(current.columns))
    if missing:
        raise SystemExit(f"Colonnes absentes de {CURRENT}: {missing}")
    current["symbol"] = current["symbol"].astype(str).str.strip()
    current["minInvestment"] = num(current["minInvestment"])
    current["currentPrice"] = num(current["currentPrice"])
    current = current[current["symbol"].ne("") & current["symbol"].notna()].copy()
    if current.empty:
        raise SystemExit("Aucune paire exploitable dans current_multicriteria.csv")

    now = current[["symbol", "currentPrice"]].dropna()
    now = now[now["currentPrice"].gt(0)]

    # Extrêmes observés dans les instantanés conservés par le projet.
    if PROJECT_TRACKER.exists():
        prior = pd.read_csv(PROJECT_TRACKER, dtype={"symbol": str})
        prior["symbol"] = prior["symbol"].astype(str).str.strip()
        prior["priceLow"] = num(prior["priceLow"])
        prior["priceHigh"] = num(prior["priceHigh"])
        observed = now.groupby("symbol", as_index=False).agg(
            observedLow=("currentPrice", "min"), observedHigh=("currentPrice", "max"))
        extrema = prior[["symbol", "priceLow", "priceHigh"]].merge(
            observed, on="symbol", how="outer")
        extrema["priceLow"] = pd.concat(
            [extrema["priceLow"], extrema["observedLow"]], axis=1).min(axis=1)
        extrema["priceHigh"] = pd.concat(
            [extrema["priceHigh"], extrema["observedHigh"]], axis=1).max(axis=1)
        extrema = extrema[["symbol", "priceLow", "priceHigh"]]
    else:
        snapshots = [read_snapshot(path) for path in HISTORY.rglob("*.csv.gz")]
        snapshots = [frame for frame in snapshots if not frame.empty]
        observations = pd.concat(snapshots + [now], ignore_index=True) if snapshots else now
        extrema = observations.groupby("symbol", as_index=False).agg(
            priceLow=("currentPrice", "min"), priceHigh=("currentPrice", "max"))
    extrema.to_csv(PROJECT_TRACKER, index=False)

    # Une ligne par paire, avec le bot dont l'investissement minimum est le plus bas.
    current = current.sort_values(["symbol", "minInvestment"], ascending=[True, True],
                                  na_position="last", kind="stable")
    pair_rows = current.drop_duplicates("symbol", keep="first")[
        ["symbol", "minInvestment", "currentPrice"]]
    result = pair_rows.merge(extrema, on="symbol", how="left")
    result = result[OUT_COLUMNS].sort_values("symbol", kind="stable")
    temp = OUTPUT.with_suffix(".csv.tmp")
    result.to_csv(temp, index=False)
    temp.replace(OUTPUT)
    print(f"{len(result)} paires -> {OUTPUT}; prix extrêmes issus de l'historique du projet.")


if __name__ == "__main__":
    main()
