#!/usr/bin/env python3
"""
build_multicriteria.py
Construit current_multicriteria.csv à partir de l'export brut (CSV ou XLSX).

 - garde uniquement les stratégies avec levier <= 1 (1 ligne par strategyId)
 - recalcule / estime les champs absents (grid profit, quantité par ordre, etc.)
 - ne conserve QUE les colonnes demandées, dans l'ordre demandé

Usage : python build_multicriteria.py entree.(csv|xlsx) [sortie.csv]
"""
import io, os, sys
import numpy as np
import pandas as pd

FEE_MID, FEE_LOW, FEE_HIGH = 0.001, 0.0015, 0.00075   # frais par jambe (spot)

COLUMNS = [
    "rank", "strategyId", "category", "strategyType", "symbol", "leverage",
    "minInvestment", "runningTime", "runningTime en J",
    "roi (fourni par Binance)", "roi (calculé)", "pnl", "matchedTrades",
    "Trades / J", "mdd7d", "gridProfit", "gridMode", "gridCount",
    "qtyPerOrderEstimate", "gridProfitEstimateLow", "gridProfitEstimateMid",
    "gridProfitEstimateHigh", "totalProfit", "gridProfitTotalProfitRatio",
    "floatingProfit", "currentPrice", "priceRange", "profitPerGridAfterFees",
    "score",
]
QUOTES = ["USDT", "USDC", "FDUSD", "BUSD", "TUSD", "USDP", "USD", "BTC", "ETH",
          "BNB", "EUR", "BRL", "TRY", "JPY"]
STABLES = {"USDT", "USDC", "FDUSD", "BUSD", "TUSD", "USDP", "USD"}


def load(path):
    df = pd.read_excel(path) if path.lower().endswith(("xlsx", "xls")) else pd.read_csv(path)
    if df.shape[1] == 1 and "," in str(df.columns[0]):      # CSV collé dans une seule colonne
        c = df.columns[0]
        df = pd.read_csv(io.StringIO(c + "\n" + "\n".join(df[c].astype(str))), index_col=False)
    return df


def quote_of(sym):
    for q in QUOTES:
        if str(sym).endswith(q):
            return q
    return None


def parse_range(s):
    try:
        a, b = str(s).split(" - ")
        return float(a), float(b)
    except Exception:
        return np.nan, np.nan


def quote_usd_rates(df):
    """Taux USD de chaque devise de cotation, lu dans le fichier lui-même
    (prix courant de BTCUSDT, ETHUSDT, BNBUSDT, EURUSDT...)."""
    rates = {q: 1.0 for q in STABLES}
    for q in ["BTC", "ETH", "BNB", "EUR", "BRL", "TRY", "JPY"]:
        s = df.loc[(df.symbol == q + "USDT") & df.currentPrice.notna(), "currentPrice"]
        if len(s):
            rates[q] = float(s.iloc[0])
            continue
        s = df.loc[(df.symbol == "USDT" + q) & df.currentPrice.notna(), "currentPrice"]
        if len(s):
            rates[q] = 1.0 / float(s.iloc[0])
    return rates


def grid_estimate(lo, hi, n, mode, price, invest_quote, matched):
    """Reconstruit l'état exact de la grille.
    - lignes de grille (arithmétiques ou géométriques)
    - capital = q * (somme des lignes d'achat sous le prix + nb de lignes de vente * prix courant)
      -> quantité par ordre q
    - profit par aller-retour = q * (pas - frais achat - frais vente)
    - grid profit = matchedTrades * profit moyen par aller-retour
    Retourne q, low, mid, high."""
    n = int(n)
    if mode == "GEOMETRIC":
        L = lo * (hi / lo) ** (np.arange(n + 1) / n)
    else:
        L = np.linspace(lo, hi, n + 1)
    p = min(max(price, lo), hi)
    need = L[L <= p].sum() + (L > p).sum() * p
    q = invest_quote / need
    steps, mids = np.diff(L), (L[:-1] + L[1:]) / 2

    def total(fee):
        return matched * q * np.mean(steps - fee * (2 * mids + steps))

    return q, total(FEE_LOW), total(FEE_MID), total(FEE_HIGH)


def main(src, dst):
    df = load(src)
    df = df.drop_duplicates("strategyId")
    df = df[df.leverage.notna() & (df.leverage <= 1)].copy()   # levier <= 1 uniquement

    rates = quote_usd_rates(df)
    df["runningTime en J"] = df.runningTime / 86400
    df["roi (fourni par Binance)"] = df.roi
    df["roi (calculé)"] = np.where(df.minInvestment > 0, df.pnl / df.minInvestment * 100, np.nan)
    df["Trades / J"] = df.matchedTrades / df["runningTime en J"]

    # champs absents -> estimation
    rng = df.priceRange.map(parse_range)
    df["_lo"], df["_hi"] = [r[0] for r in rng], [r[1] for r in rng]
    df["_q"] = df.symbol.map(quote_of)
    for c in ["gridProfitEstimateLow", "gridProfitEstimateMid", "gridProfitEstimateHigh", "qtyPerOrderEstimate"]:
        df[c] = df[c].astype(float) if c in df else np.nan

    for i, r in df.iterrows():
        have_src = pd.notna(r.get("gridProfitEstimateMid"))
        rate = rates.get(r._q)
        ok = (r.gridCount > 0 and r._lo > 0 and r._hi > r._lo and r.currentPrice > 0
              and r.roi > 0 and r.pnl > 0 and r.matchedTrades > 0 and rate)
        if not ok:
            continue
        # capital réellement investi (USD) = pnl / roi : cohérent avec les deux champs Binance
        invest_quote = (r.pnl / (r.roi / 100)) / rate
        q, lo_, mid_, hi_ = grid_estimate(r._lo, r._hi, r.gridCount, r.gridMode, r.currentPrice,
                                          invest_quote, r.matchedTrades)
        df.loc[i, "qtyPerOrderEstimate"] = q
        df.loc[i, ["gridProfitEstimateLow", "gridProfitEstimateMid", "gridProfitEstimateHigh"]] = \
            [lo_ * rate, mid_ * rate, hi_ * rate]                      # convertis en USD

    gp = df.gridProfit.where(df.gridProfit.notna(), df.gridProfitEstimateMid)  # officiel > estimé
    df["totalProfit"] = df.totalProfit.where(df.totalProfit.notna(), df.pnl)
    df["gridProfitTotalProfitRatio"] = np.where(df.totalProfit > 0, gp / df.totalProfit, np.nan)
    df["floatingProfit"] = df.totalProfit - gp

    out = df.sort_values("score", ascending=False)[COLUMNS]
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    out.to_csv(dst, index=False)
    print(f"{len(out)} stratégies (levier <= 1) -> {dst}")
    print("Grid profit estimé :", int(out.gridProfitEstimateMid.notna().sum()),
          "| non estimable (grille inconnue) :", int(out.gridProfitEstimateMid.isna().sum()))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "results/current_multicriteria_enriched.csv")
