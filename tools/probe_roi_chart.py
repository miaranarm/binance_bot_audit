import json
from playwright.sync_api import sync_playwright

ENDPOINT = "https://www.binance.com/bapi/futures/v1/public/future/common/strategy/landing-page/queryRoiChart"

def walk_points(value, out):
    if isinstance(value, dict):
        keys = {str(k).replace("_", "").lower(): k for k in value}
        rk = next((keys[k] for k in ("roi", "roipct", "roirate") if k in keys), None)
        pk = next((keys[k] for k in ("pnl", "profitloss", "totalpnl") if k in keys), None)
        if rk is not None and pk is not None:
            try:
                roi = float(value[rk])
                pnl = float(value[pk])
                out.append({"roi": roi, "pnl": pnl})
            except (TypeError, ValueError):
                pass
        for v in value.values():
            walk_points(v, out)
    elif isinstance(value, list):
        for v in value:
            walk_points(v, out)

def main():
    ids = ["3232564", "9161957", "8799020", "101349660"]
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        for sid in ids:
            try:
                r = page.request.post(
                    ENDPOINT,
                    data={"strategyId": sid, "streamerStrategyType": "SPOT_GRID"},
                    timeout=30000,
                )
                item = {"status": r.status, "url": r.url}
                if r.ok:
                    payload = r.json()
                    points = []
                    walk_points(payload, points)
                    item["point_count"] = len(points)
                    item["last_points"] = points[-5:]
                    if points and points[-1]["roi"]:
                        last = points[-1]
                        item["investment_from_chart"] = last["pnl"] / (last["roi"] / 100.0)
                    item["raw"] = payload
                else:
                    item["text"] = r.text()[:1000]
                results[sid] = item
            except Exception as exc:
                results[sid] = {"error": str(exc)}
        browser.close()
    with open("roi_chart_probe.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: {x:y for x,y in v.items() if x != "raw"} for k,v in results.items()}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
