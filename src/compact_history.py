#!/usr/bin/env python3
"""Migration unique et idempotente de results/history/ vers le format allégé.

Avant : un CSV complet (36 colonnes, ~9 700 bots, ~1,3 Mo) par scan horaire => ~900 Mo par mois.
Après : - results/history/      : (symbol, currentPrice) une ligne par paire, quelques Ko par scan, conservé sans purge ;
        - results/history_bots/ : 11 colonnes utiles bot par bot, un instantané toutes les 3 h.

Usage : python src/compact_history.py [--dry-run]
Relancer le script est sans danger : les fichiers déjà allégés (sans colonne strategyId) sont ignorés.
"""
import gzip
import io
import re
import sys
from pathlib import Path

import pandas as pd

RESULTS = Path("results")
HISTORY = RESULTS / "history"
BOTS = RESULTS / "history_bots"
BOT_COLS = ["strategyId", "symbol", "minInvestment", "runningTime", "pnl", "matchedTrades",
            "mdd7d", "gridMode", "gridCount", "currentPrice", "priceRange"]
STEP_H = 3


def write_gz(path, frame):
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=9, mtime=0) as handle:
        handle.write(frame.to_csv(index=False).encode("utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buffer.getvalue())


def main(dry_run=False):
    files = sorted(HISTORY.rglob("*.csv.gz"))
    seen_buckets = {p.name[:8] + str(int(p.name[9:11]) // STEP_H) for p in BOTS.rglob("*.csv.gz")}
    before = after = converted = 0
    for path in files:
        match = re.search(r"(\d{8})T(\d{2})\d{4}Z", path.name)
        if not match:
            continue
        before += path.stat().st_size
        try:
            frame = pd.read_csv(path, low_memory=False)
        except Exception as exc:  # fichier corrompu : on le laisse tel quel
            print(f"IGNORE {path}: {exc}")
            after += path.stat().st_size
            continue
        if "strategyId" not in frame.columns:      # déjà allégé
            after += path.stat().st_size
            continue
        pcol = "currentPrice" if "currentPrice" in frame.columns else ("price" if "price" in frame.columns else None)
        if pcol is None or "symbol" not in frame.columns:   # schéma inconnu : on ne touche pas au fichier
            after += path.stat().st_size
            continue
        prices = frame[["symbol", pcol]].copy()
        prices["symbol"] = prices["symbol"].astype(str).str.strip()
        prices[pcol] = pd.to_numeric(prices[pcol], errors="coerce")
        prices = prices[prices[pcol].gt(0)].groupby("symbol", as_index=False)[pcol].median()
        bucket = match.group(1) + str(int(match.group(2)) // STEP_H)
        keep_bots = bucket not in seen_buckets
        if keep_bots:
            seen_buckets.add(bucket)
        if not dry_run:
            if keep_bots:
                folder = BOTS / path.parent.relative_to(HISTORY)
                write_gz(folder / path.name, frame[[c for c in BOT_COLS if c in frame.columns]])
            write_gz(path, prices)
            after += path.stat().st_size
        converted += 1
    bots_size = sum(p.stat().st_size for p in BOTS.rglob("*.csv.gz"))
    print(f"{converted} instantanés convertis | history/ : {before / 1e6:.0f} Mo -> {after / 1e6:.1f} Mo"
          f" | history_bots/ : {bots_size / 1e6:.1f} Mo" + (" (simulation)" if dry_run else ""))


if __name__ == "__main__":
    main("--dry-run" in sys.argv)
