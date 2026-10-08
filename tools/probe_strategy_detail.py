import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

STRATEGY_IDS = ["3232564"]
DETAIL_URL = "https://www.binance.com/bapi/algo/v1/friendly/algo/public/strategy/detail"
PAGE_URL = "https://www.binance.com/en/trading-bots/spot/grid/XRPBTC"


async def main():
    out = Path("results/strategy_detail_raw.json")
    out.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            locale="en-US",
            extra_http_headers={
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.binance.com",
                "Referer": PAGE_URL,
            },
        )
        page = await context.new_page()
        intercepted = []

        async def capture_response(response):
            if "bapi/algo" not in response.url:
                return
            try:
                body = await response.text()
                if len(intercepted) < 100:
                    intercepted.append({
                        "url": response.url,
                        "status": response.status,
                        "method": response.request.method,
                        "body": body[:200000],
                    })
            except Exception as exc:
                intercepted.append({
                    "url": response.url,
                    "status": response.status,
                    "method": response.request.method,
                    "error": repr(exc),
                })

        page.on("response", capture_response)

        navigation = {}
        try:
            resp = await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
            navigation = {
                "status": resp.status if resp else None,
                "url": resp.url if resp else PAGE_URL,
            }
        except Exception as exc:
            navigation = {"error": repr(exc)}

        await page.wait_for_timeout(8000)

        results = {
            "navigation": navigation,
            "intercepted_bapi_algo": intercepted,
            "direct_fetch": {},
        }

        for sid in STRATEGY_IDS:
            try:
                results["direct_fetch"][sid] = await page.evaluate(
                    """async ({url, sid}) => {
                        try {
                            const r = await fetch(url, {
                                method: "POST",
                                credentials: "include",
                                headers: {
                                    "accept": "application/json, text/plain, */*",
                                    "content-type": "application/json",
                                    "clienttype": "web",
                                    "lang": "en",
                                    "origin": "https://www.binance.com",
                                    "referer": "https://www.binance.com/en/trading-bots/spot/grid/XRPBTC"
                                },
                                body: JSON.stringify({strategyId: sid})
                            });
                            const text = await r.text();
                            let body = null;
                            try { body = JSON.parse(text); } catch (_) {}
                            return {
                                status: r.status,
                                statusText: r.statusText,
                                contentType: r.headers.get("content-type"),
                                text: text.slice(0, 200000),
                                body
                            };
                        } catch (e) {
                            return {error: String(e), stack: e?.stack || null};
                        }
                    }""",
                    {"url": DETAIL_URL, "sid": sid},
                )
            except Exception as exc:
                results["direct_fetch"][sid] = {"python_error": repr(exc)}

        await browser.close()

    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(out),
        "navigation": navigation,
        "intercepted_count": len(intercepted),
        "direct_fetch": results["direct_fetch"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
