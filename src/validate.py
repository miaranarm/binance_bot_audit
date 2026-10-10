#!/usr/bin/env python3
"""src/validate.py -- contrôles du pipeline (remplace validate_results.py et validate_final_csv.py).

  python src/validate.py raw   [results/current_multicriteria.csv]   # après audit_all.py
  python src/validate.py final [results/current_multicriteria.csv]   # après build_multicriteria.py
Code de sortie 1 en cas d'erreur.
"""
import csv
import json
import math
import sys
from pathlib import Path

TOL = 2e-5
EXACT_RATIO_SOURCES = {"BINANCE_DETAIL_GRID_DIV_TOTAL_EXACT", "BINANCE_DETAIL_TOTAL_MINUS_FLOATING_DIV_TOTAL_EXACT"}
ESTIMATED_RATIO_SOURCE = "RECONSTRUCTED_GRID_PROFIT_DIV_BINANCE_MARKETPLACE_TOTAL_PROFIT"
FINAL_COLUMNS = [
    "rank", "strategyId", "category", "strategyType", "symbol", "leverage",
    "minInvestment", "runningTime", "runningTime en J",
    "roi (fourni par Binance)", "roi (calculé)", "pnl", "matchedTrades",
    "Trades / J", "mdd7d", "gridProfit", "gridMode", "gridCount",
    "qtyPerOrderEstimate", "gridProfitEstimateLow", "gridProfitEstimateMid",
    "gridProfitEstimateHigh", "totalProfit", "gridProfitTotalProfitRatio",
    "floatingProfit", "currentPrice", "priceRange", "profitPerGridAfterFees",
    "score",
]


def num(value):
    try:
        number = float(str(value).strip().replace("≈", ""))
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def check_raw(path):
    """Cohérence provenance / ratio / floating du CSV brut. Retourne (stats, exemples)."""
    with Path(path).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    stats = dict.fromkeys(("reconstructed", "exact", "unavailable", "unknownSource", "orderingErrors",
                           "ratioErrors", "floatingErrors", "sourceErrors", "negativeEstimateRows",
                           "ratiosOver100Percent", "estimatedRatiosOver100Percent"), 0)
    stats["rows"] = len(rows)
    examples = []

    def bad(kind, key, row, **extra):
        stats[key] += 1
        if len(examples) < 10:
            examples.append({"strategyId": row.get("strategyId"), "type": kind, **extra})

    for row in rows:
        source = row.get("gridProfitSource", "").strip()
        r_source = row.get("gridProfitTotalProfitRatioSource", "").strip()
        r_status = row.get("gridProfitTotalProfitRatioStatus", "").strip()
        g = lambda k: num(row.get(k))
        grid, low, mid, high = g("gridProfit"), g("gridProfitEstimateLow"), g("gridProfitEstimateMid"), g("gridProfitEstimateHigh")
        total, ratio, floating = g("totalProfit"), g("gridProfitTotalProfitRatio"), g("floatingProfit")
        rlow, rmid, rhigh = (g(f"gridProfitTotalProfitRatioEstimate{s}") for s in ("Low", "Mid", "High"))
        ordered = None not in (low, mid, high) and low <= mid + TOL and mid <= high + TOL

        if source == "RECONSTRUCTED":
            stats["reconstructed"] += 1
            if grid is None or floating is not None:
                bad("BAD_RECONSTRUCTED_METRIC_STATE", "sourceErrors", row, gridProfit=grid, floatingProfit=floating)
            if r_status != "ESTIMATED_NOT_EXACT" or r_source != ESTIMATED_RATIO_SOURCE:
                bad("BAD_RECONSTRUCTED_RATIO_PROVENANCE", "sourceErrors", row, status=r_status, source=r_source)
            if ratio is None:
                bad("MISSING_PRIMARY_ESTIMATED_RATIO", "sourceErrors", row)
            elif rmid is not None and abs(ratio - rmid) > 1e-6:
                bad("PRIMARY_RATIO_MISMATCH", "ratioErrors", row, ratio=ratio, mid=rmid)
            if any(v is not None and v < 0 for v in (low, mid, high)):
                stats["negativeEstimateRows"] += 1       # économiquement possible (frais > pas de grille) : conservé
            if not ordered:
                bad("GRID_ESTIMATE_ORDER", "orderingErrors", row, low=low, mid=mid, high=high)
            if None not in (rlow, rmid, rhigh) and not (rlow <= rmid + TOL and rmid <= rhigh + TOL):
                bad("RATIO_ESTIMATE_ORDER", "orderingErrors", row, low=rlow, mid=rmid, high=rhigh)
            if rmid is not None and rmid > 1:
                stats["estimatedRatiosOver100Percent"] += 1
        elif source == "BINANCE_EXACT":
            stats["exact"] += 1
            if grid is None:
                bad("EXACT_MISSING_GRIDPROFIT", "sourceErrors", row)
                continue
            if None in (low, mid, high) or any(abs(v - grid) > TOL for v in (low, mid, high)):
                bad("EXACT_ESTIMATE_MISMATCH", "sourceErrors", row)
            if r_source not in EXACT_RATIO_SOURCES or r_status != "EXACT":
                bad("BAD_EXACT_RATIO_PROVENANCE", "sourceErrors", row, source=r_source, status=r_status)
            if row.get("totalProfitSource", "") != "BINANCE_DETAIL_TOTAL_PROFIT":
                bad("EXACT_WITH_NON_DETAIL_TOTAL", "sourceErrors", row, totalSource=row.get("totalProfitSource"))
            if total not in (None, 0):
                if ratio is None or abs(ratio - grid / total) > TOL:
                    bad("EXACT_RATIO_MISMATCH", "ratioErrors", row, ratio=ratio, expected=grid / total)
                if floating is not None and abs(floating - (total - grid)) > TOL * max(1.0, abs(total)):
                    bad("EXACT_FLOATING_MISMATCH", "floatingErrors", row, floating=floating, expected=total - grid)
            if ratio is not None and ratio > 1:
                stats["ratiosOver100Percent"] += 1
        elif source in ("", "UNAVAILABLE"):
            stats["unavailable"] += 1
            if grid is not None or floating is not None:
                bad("UNAVAILABLE_WITH_VALUES", "sourceErrors", row)
        else:
            bad("UNKNOWN_SOURCE", "unknownSource", row, source=source)

    errors = ("orderingErrors", "ratioErrors", "floatingErrors", "sourceErrors", "unknownSource")
    stats["status"] = "FAIL" if any(stats[k] for k in errors) else "PASS"
    return stats, examples


def check_final(path):
    """Schéma figé (29 colonnes), unicité, levier <= 1 et cohérence des calculs. Retourne un message."""
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != FINAL_COLUMNS:
            raise ValueError(f"CSV schema mismatch\nExpected: {FINAL_COLUMNS}\nActual: {reader.fieldnames}")
        rows = list(reader)
    if not rows:
        raise ValueError("no bots in final CSV")
    ids = [str(r.get("strategyId", "")).strip() for r in rows]
    if not all(ids) or len(ids) != len(set(ids)):
        raise ValueError("empty or duplicate strategyId")

    for r in rows:
        sid = r["strategyId"]
        if r.get("rank") or r.get("score"):
            raise ValueError(f"rank/score must stay empty ({sid})")
        lev = num(r.get("leverage"))
        if lev is None or lev > 1:
            raise ValueError(f"invalid leverage for {sid}: {r.get('leverage')}")
        runtime, days = num(r.get("runningTime")), num(r.get("runningTime en J"))
        if runtime is not None and days is not None and abs(days - runtime / 86400) > 1e-7:
            raise ValueError(f"runtime/day mismatch for {sid}")
        trades, per_day = num(r.get("matchedTrades")), num(r.get("Trades / J"))
        if runtime and runtime > 0 and trades is not None and per_day is not None \
                and abs(per_day - trades / (runtime / 86400)) > max(1e-6, abs(per_day) * 1e-6):
            raise ValueError(f"Trades / J mismatch for {sid}")

        low, mid, high = (num(r.get(k)) for k in ("gridProfitEstimateLow", "gridProfitEstimateMid", "gridProfitEstimateHigh"))
        if None not in (low, mid, high) and not (low <= mid + 1e-6 and mid <= high + 1e-6):
            raise ValueError(f"Grid Profit interval unordered for {sid}")
        grid, total, floating = num(r.get("gridProfit")), num(r.get("totalProfit")), num(r.get("floatingProfit"))
        ratio_raw = str(r.get("gridProfitTotalProfitRatio", "")).strip()
        ratio = num(ratio_raw)
        if grid is not None and None not in (low, mid, high) \
                and any(abs(v - grid) > max(1e-6, abs(grid) * 1e-6) for v in (low, mid, high)):
            raise ValueError(f"exact Grid Profit differs from its interval for {sid}")
        if ratio is not None and total == 0:
            raise ValueError(f"ratio must be blank when Total Profit is zero for {sid}")
        if grid is None:
            if ratio is not None and not ratio_raw.startswith("≈"):
                raise ValueError(f"estimated ratio must be marked with ≈ for {sid}")
            if floating is not None:
                raise ValueError(f"floatingProfit derived from estimated Grid Profit for {sid}")
            if ratio is not None and total and mid is not None \
                    and abs(ratio - mid / total) > max(1e-4, abs(mid / total) * 1e-4):
                raise ValueError(f"estimated Grid/Total ratio mismatch for {sid}")
        else:
            if ratio is not None and total and abs(ratio - grid / total) > max(1e-6, abs(ratio) * 1e-6):
                raise ValueError(f"Grid/Total ratio mismatch for {sid}")
            if floating is not None and total is not None and abs(total - grid - floating) > max(1e-6, abs(total) * 1e-6):
                raise ValueError(f"floatingProfit identity mismatch for {sid}")

    exact = sum(num(r.get("gridProfit")) is not None for r in rows)
    ppg = sum(bool(r.get("profitPerGridAfterFees")) for r in rows)
    return f"PASS: {len(rows)} unique bots, leverage <= 1, {len(FINAL_COLUMNS)} columns, {exact} exact Grid Profit, {ppg} with profit/grid"


def main(argv):
    if len(argv) < 2 or argv[1] not in ("raw", "final"):
        raise SystemExit(__doc__)
    path = argv[2] if len(argv) > 2 else "results/current_multicriteria.csv"
    if not Path(path).exists() or Path(path).stat().st_size == 0:
        raise SystemExit(f"ERROR: {path} missing or empty")
    if argv[1] == "final":
        try:
            print(check_final(path))
        except ValueError as exc:
            raise SystemExit(f"ERROR: {exc}")
        return
    stats, examples = check_raw(path)
    print(json.dumps({"validation": stats, "examples": examples}, ensure_ascii=False))
    meta = Path(path).parent / "scan_meta.json"
    if meta.exists():                      # le rapport de validation est consigné dans scan_meta.json
        data = json.loads(meta.read_text(encoding="utf-8"))
        data["validation"] = {**stats, "examples": examples}
        meta.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if stats["status"] == "FAIL":
        raise SystemExit(1)


if __name__ == "__main__":
    main(sys.argv)
