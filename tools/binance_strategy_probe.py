#!/usr/bin/env python3
"""Interroge un endpoint public du Marketplace Binance pour un strategyId.

Exemples :
  python binance_strategy_probe.py 10142665
  python binance_strategy_probe.py 10142665 --endpoint queryRoiChart
  python binance_strategy_probe.py 10142665 --endpoint AUTRE_NOM --raw

Le script ne demande aucune cle API et n'envoie aucun cookie.
Non teste depuis l'environnement de Claude (pas d'acces reseau).
"""
import argparse
import json
import sys
import time

import requests

BASE = "https://www.binance.com"  # essaie aussi https://www.binance.bh si besoin
PATH = "/bapi/futures/v1/public/future/common/strategy/landing-page/"
HEADERS = {
    "Content-Type": "application/json",
    "lang": "en",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/154.0.0.0 Safari/537.36",
}


def call(endpoint, strategy_id, base=BASE, strategy_type="SPOT_GRID"):
    url = base + PATH + endpoint
    payload = {"strategyId": int(strategy_id), "streamerStrategyType": strategy_type}
    r = requests.post(url, json=payload, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def find_keys(obj, words=("grid", "profit", "float", "invest"), path=""):
    """Parcourt le JSON et liste les champs dont le nom contient un des mots."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if any(w in k.lower() for w in words):
                found.append((p, v))
            found += find_keys(v, words, p)
    elif isinstance(obj, list) and obj:
        found += find_keys(obj[-1], words, path + "[-1]")
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("strategy_id")
    ap.add_argument("--endpoint", default="queryRoiChart")
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--raw", action="store_true", help="affiche le JSON complet")
    a = ap.parse_args()

    try:
        data = call(a.endpoint, a.strategy_id, a.base)
    except requests.RequestException as e:
        sys.exit(f"Erreur reseau/HTTP : {e}")

    if a.raw:
        print(json.dumps(data, indent=2, ensure_ascii=False))

    print("success:", data.get("success"), "| code:", data.get("code"))

    # Cas du graphique ROI/PNL : dernier point = etat actuel
    rows = data.get("data")
    if isinstance(rows, list) and rows and isinstance(rows[-1], dict):
        last = rows[-1]
        roi, pnl = last.get("roi"), last.get("pnl")
        print(f"Dernier point -> ROI: {roi}  PNL total: {pnl}")
        if roi:
            print(f"Investissement estime (PNL/ROI): {pnl / roi:,.0f}")
            print("Note : ROI arrondi cote Binance -> estimation approximative.")

    # Cherche tout champ qui parle de grid / profit
    print("\nChamps contenant grid/profit/float/invest :")
    hits = find_keys(data)
    if hits:
        for p, v in hits:
            print(f"  {p} = {v}")
    else:
        print("  aucun (ce endpoint ne contient pas le Grid Profit)")

    time.sleep(1)  # politesse envers le serveur


if __name__ == "__main__":
    main()
