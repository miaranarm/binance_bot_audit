#!/usr/bin/env python3
"""src/grid_sim.py -- répartition des cycles achat/vente d'une grille Spot sur le parcours réel du prix.

Pourquoi : le Grid Profit = nombre de trades appariés x profit net moyen *des grilles réellement traversées*
x quantité par ordre. L'ancienne estimation moyennait toutes les grilles de la plage, y compris celles que le
prix n'a jamais visitées, d'où des ratios Grid/Total > 100 % dans 38 % des cas. Ici, on rejoue le parcours
(bougies publiques Binance) sur la grille pour savoir OÙ les cycles ont eu lieu ; le NOMBRE de cycles reste
celui de Binance (matchedTrades), ce qui absorbe la résolution limitée des bougies.
"""
import math

import numpy as np

# (durée max en jours, intervalle des bougies, durée d'une bougie en ms)
INTERVALS = ((7, "5m", 300_000), (30, "15m", 900_000), (120, "1h", 3_600_000), (10**6, "4h", 14_400_000))


def pick_interval(days):
    return next((name, ms) for limit, name, ms in INTERVALS if days <= limit)


def parse_klines(rows):
    """Réponse /api/v3/klines -> tableau float (n, 5) : openTime, open, high, low, close."""
    out = [(float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows or []]
    return np.array(out, dtype=float).reshape(-1, 5)


def price_path(klines, start_ms):
    """Points successifs du prix à partir de start_ms : O, puis L/H dans l'ordre le plus probable, puis C."""
    k = klines[klines[:, 0] >= start_ms - 1]
    if not len(k):
        return np.empty(0), None
    o, h, l, c = k[:, 1], k[:, 2], k[:, 3], k[:, 4]
    up = c >= o                                   # bougie haussière : creux avant sommet
    first, second = np.where(up, l, h), np.where(up, h, l)
    return np.column_stack((o, first, second, c)).ravel(), float(o[0])


def cycles_per_cell(path, start, lower, upper, n, mode):
    """Cycles achat->vente complétés dans chacune des n cellules de la grille (np.array de taille n)."""
    if len(path) < 2 or not (0 < lower < upper) or n < 2:
        return np.zeros(max(n, 0))
    if mode == "GEOMETRIC":
        coord = lambda p: np.log(np.maximum(p, 1e-300) / lower) / (math.log(upper / lower) / n)
    else:
        coord = lambda p: (p - lower) / ((upper - lower) / n)
    idx = np.clip(np.floor(coord(path)), -1, n).astype(int)      # -1 sous la plage, n au-dessus
    a, b = idx[:-1], idx[1:]
    t = np.arange(len(a))

    def expand(first, count):     # pour chaque mouvement, les niveaux franchis first..first+count-1
        total = int(count.sum())
        move = np.repeat(np.arange(len(count)), count)
        offset = np.arange(total) - np.repeat(np.cumsum(count) - count, count)
        return move, first[move] + offset

    up, dn = b > a, b < a
    mu, lu = expand(a[up] + 1, (b - a)[up])              # hausse : niveaux a+1..b -> vente de la cellule k-1
    md, ld = expand(b[dn] + 1, (a - b)[dn])              # baisse : niveaux b+1..a -> achat de la cellule k
    keep_u, keep_d = (lu >= 1) & (lu <= n), (ld >= 0) & (ld <= n - 1)
    s0 = coord(np.array([start]))[0]
    init_cells = np.flatnonzero(np.arange(n) + 1 > s0)   # lot initial : niveau haut de la cellule > prix de départ
    cell = np.concatenate((init_cells, ld[keep_d], lu[keep_u] - 1))
    time = np.concatenate((np.full(len(init_cells), -1), t[dn][md[keep_d]], t[up][mu[keep_u]]))
    kind = np.concatenate((np.zeros(len(init_cells) + keep_d.sum(), int), np.ones(keep_u.sum(), int)))  # 0 achat, 1 vente
    order = np.lexsort((time, cell))
    cell, kind = cell[order], kind[order]
    done = (cell[:-1] == cell[1:]) & (kind[:-1] == 0) & (kind[1:] == 1)        # achat puis vente consécutifs
    return np.bincount(cell[:-1][done], minlength=n).astype(float)


def weighted_net(cycles, levels, fee):
    """Profit net moyen par cycle, pondéré par les cycles observés (unité de cotation par unité de base)."""
    net = np.array([(levels[i + 1] - levels[i]) - fee * (levels[i] + levels[i + 1]) for i in range(len(cycles))])
    total = cycles.sum()
    return (float((cycles * net).sum() / total), float(total)) if total > 0 else (None, 0.0)
