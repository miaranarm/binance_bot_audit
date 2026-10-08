import asyncio, json
from pathlib import Path
from playwright.async_api import async_playwright

STRATEGY_IDS = ["3232564"]

async def main():
    out = Path("results/strategy_detail_raw.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://www.binance.com/en/trading-bots/spot/grid/XRPBTC", wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        results = {}
        for sid in STRATEGY_IDS:
            results[sid] = await page.evaluate("""async (sid) => {
                const r = await fetch("https://www.binance.com/bapi/algo/v1/friendly/algo/public/strategy/detail", {
                    method: "POST",
                    credentials: "include",
                    headers: {
                        "content-type": "application/json",
                        "clienttype": "web",
                        "lang": "en"
                    },
                    body: JSON.stringify({strategyId: sid})
                });
                return {status: r.status, body: await r.json()};
            }""", sid)
        await browser.close()
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out)

if __name__ == "__main__":
    asyncio.run(main())
