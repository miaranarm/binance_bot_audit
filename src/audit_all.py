import csv
import datetime as dt
import gzip
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("results")
HISTORY = OUT / "history"
OUT.mkdir(parents=True, exist_ok=True)
HISTORY.mkdir(parents=True, exist_ok=True)

BASE = "https://www.binance.com/bapi/futures/v1/public/future/common/strategy/landing-page/"
TOP = BASE + "queryTopStrategy"
DCA = BASE + "queryTopUmDcaStrategy"

FIELDS = [
    "rank", "strategyId", "category", "strategyType", "symbol", "leverage",
    "minInvestment", "runningTime", "roi", "pnl", "matchedTrades", "mdd7d", "score"
]

RETENTION_DAYS = 30


def num(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def leverage(x):
    params = x.get("strategyParams") or {}
    value = params.get("leverage", x.get("leverage"))
    if value in (None, "", "null"):
        return 1.0
    try:
        return float(value)
    except Exception:
        return 99.0


def pages(page, endpoint, base_query, category, streamer, diagnostics):
    rows = []
    for page_no in range(1, 150):
        query = dict(base_query)
        query.update(page=page_no, rows=100)
        try:
            response = page.request.post(endpoint, data=query).json()
            data = response.get("data") or []
        except Exception as exc:
            diagnostics.append({
                "endpoint": endpoint,
                "category": category,
                "page": page_no,
                "error": str(exc),
            })
            break

        for item in data:
            item = dict(item)
            item["_category"] = category
            item["_streamer"] = streamer
            item["_lev"] = leverage(item)
            rows.append(item)

        total = int(response.get("total") or 0)
        if len(data) < 100 or page_no * 100 >= total:
            break

    return rows


def build_current(rows):
    current = []
    seen = set()

    for x in rows:
        sid = str(x.get("strategyId") or "").strip()
        symbol = str(x.get("symbol") or "").strip()
        lev = num(x.get("_lev"), 99.0)

        # Only strategies currently published by Binance and tradable from
        # the public Bot Marketplace, with leverage <= 1.
        if not sid or not symbol or lev > 1.0 or sid in seen:
            continue
        seen.add(sid)

        item = {
            "strategyId": sid,
            "category": x.get("_category", ""),
            "strategyType": x.get("strategyType", ""),
            "symbol": symbol,
            "leverage": lev,
            "minInvestment": x.get("minInvestment", x.get("minimumInvestment", "")),
            "runningTime": x.get("runningTime", ""),
            "roi": num(x.get("roi", x.get("roiPct", x.get("roiRate", 0)))),
            "pnl": num(x.get("pnl", x.get("profitLoss", x.get("totalPnl", 0)))),
            "matchedTrades": num(x.get("matchedTrades", x.get("matchedCount", x.get("totalMatchedTrades", 0)))),
            "mdd7d": num(x.get("mdd7d", x.get("sevenDayMdd", x.get("7dMdd", 0)))),
        }
        current.append(item)

    # Same scoring model as the previous scanner, but only across the
    # currently tradable <=1x universe.
    config = [
        ("roi", 0.30, 1),
        ("pnl", 0.25, 1),
        ("runningTime", 0.15, 1),
        ("matchedTrades", 0.15, 1),
        ("mdd7d", 0.15, -1),
    ]

    for item in current:
        score = 0.0
        for field, weight, direction in config:
            values = [num(x[field]) for x in current]
            lo, hi = min(values, default=0.0), max(values, default=0.0)
            normalized = (num(item[field]) - lo) / (hi - lo) if hi > lo else 0.5
            score += weight * normalized * direction
        item["score"] = 100.0 * score

    current.sort(key=lambda x: x["score"], reverse=True)
    return current


def write_current(current):
    path = OUT / "current_multicriteria.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for rank, item in enumerate(current, 1):
            writer.writerow({"rank": rank, **item})


def write_summary(current):
    path = OUT / "current_multicriteria_summary.txt"
    with path.open("w", encoding="utf-8") as handle:
        handle.write(f"COUNT={len(current)}\n")
        handle.write("UNIVERSE=Binance Bot Marketplace active strategies\n")
        handle.write("FILTER=leverage<=1 and non-empty tradable symbol\n")
        handle.write("SCORING=ROI30 PNL25 RUNTIME15 MATCHED15 LOW_7D_MDD15\n")
        for rank, item in enumerate(current[:20], 1):
            handle.write(
                f"{rank}. {item['strategyId']} {item['symbol']} "
                f"score={item['score']:.4f} roi={item['roi']} pnl={item['pnl']} "
                f"trades={item['matchedTrades']} mdd7d={item['mdd7d']}\n"
            )


def write_history(current, now):
    # One compact gzip snapshot per hourly scan. Filename is the timestamp,
    # so retention is independent of Git checkout file mtimes.
    folder = HISTORY / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = folder / f"{now.strftime('%Y%m%dT%H%M%SZ')}.csv.gz"

    with gzip.open(snapshot, "wt", newline="", encoding="utf-8", compresslevel=9) as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for rank, item in enumerate(current, 1):
            writer.writerow({"rank": rank, **item})

    cutoff = now - dt.timedelta(days=RETENTION_DAYS)
    removed = 0
    for path in HISTORY.rglob("*.csv.gz"):
        try:
            stamp = dt.datetime.strptime(path.name[:16], "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc)
        except Exception:
            continue
        if stamp < cutoff:
            path.unlink(missing_ok=True)
            removed += 1

    # Remove empty date directories after retention cleanup.
    for directory in sorted(HISTORY.glob("*/*/*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()

    return snapshot, removed


def main():
    diagnostics = []
    now = dt.datetime.now(dt.timezone.utc)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        rows = pages(
            page, TOP,
            {"strategyType": 1, "symbol": "", "zone": "", "sort": "pnl"},
            "Spot Grid", "SPOT_GRID", diagnostics,
        )

        # Binance currently exposes multiple bot families through this
        # landing-page endpoint. Keep only strategies that actually appear
        # in the active public marketplace and pass the <=1x filter below.
        for strategy_type in range(2, 21):
            rows.extend(
                pages(
                    page, TOP,
                    {"strategyType": strategy_type, "symbol": "", "zone": "", "sort": "pnl"},
                    f"Marketplace type {strategy_type}", f"TYPE_{strategy_type}", diagnostics,
                )
            )

        rows.extend(
            pages(
                page, DCA,
                {
                    "market": "", "zone": "", "roi": "", "sort": "pnl",
                    "trailingType": "", "leverage": "", "investmentType": False,
                    "sevenDayMdd": "", "strategyType": 10, "symbol": "",
                },
                "Futures DCA", "UM_DCA", diagnostics,
            )
        )

        browser.close()

    current = build_current(rows)
    write_current(current)
    write_summary(current)
    snapshot, removed = write_history(current, now)

    with (OUT / "scan_meta.json").open("w", encoding="utf-8") as handle:
        json.dump({
            "scan_utc": now.isoformat(),
            "raw_rows": len(rows),
            "tradable_leverage_le_1": len(current),
            "history_retention_days": RETENTION_DAYS,
            "snapshot": str(snapshot),
            "old_snapshots_removed": removed,
            "binance_marketplace_refresh": "hourly",
            "fields": FIELDS,
        }, handle, ensure_ascii=False, indent=2)

    with (OUT / "type_census.json").open("w", encoding="utf-8") as handle:
        json.dump({
            "endpoint_diagnostics": diagnostics,
            "filter": "active public Binance Bot Marketplace strategies with leverage <= 1",
        }, handle, ensure_ascii=False, indent=2)

    print(f"DONE raw={len(rows)} current={len(current)} snapshot={snapshot} removed={removed}")


if __name__ == "__main__":
    main()
