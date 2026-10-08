import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

PAGE_URLS = [
    "https://www.binance.com/en/trading-bots/spot/grid/XRPBTC",
    "https://www.binance.com/en/trading-bots/spot/grid/XRPUSDT",
]
KEYWORDS = (
    "bapi", "api/", "algo", "strategy", "bot", "marketplace", "grid",
    "trading-bots", "mdd", "profit", "pnl"
)

async def main():
    out = Path("results/marketplace_network_raw.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    captured = []
    console = []
    pages = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            locale="en-US",
            timezone_id="Africa/Nairobi",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/154.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            },
        )
        page = await context.new_page()

        async def on_response(response):
            url = response.url
            typ = response.request.resource_type
            interesting = typ in ("xhr", "fetch") or any(k in url.lower() for k in KEYWORDS)
            if not interesting or len(captured) >= 500:
                return
            item = {
                "url": url,
                "status": response.status,
                "method": response.request.method,
                "resource_type": typ,
                "request_headers": {},
                "response_headers": {},
            }
            try:
                item["request_headers"] = await response.request.all_headers()
            except Exception:
                pass
            try:
                item["response_headers"] = await response.all_headers()
            except Exception:
                pass
            try:
                body = await response.text()
                item["body"] = body[:300000]
            except Exception as exc:
                item["body_error"] = repr(exc)
            captured.append(item)

        async def on_console(msg):
            if len(console) < 200:
                console.append({"type": msg.type, "text": msg.text[:5000]})

        page.on("response", on_response)
        page.on("console", on_console)

        for url in PAGE_URLS:
            info = {"url": url}
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=90000)
                info["status"] = resp.status if resp else None
                info["final_url"] = page.url
            except Exception as exc:
                info["error"] = repr(exc)

            await page.wait_for_timeout(20000)

            try:
                info["title"] = await page.title()
                info["html_len"] = len(await page.content())
                info["text_sample"] = (await page.locator("body").inner_text())[:10000]
                info["resources"] = await page.evaluate(
                    """() => performance.getEntriesByType('resource').map(e => ({
                        name:e.name, initiatorType:e.initiatorType
                    })).filter(x => x.name.includes('binance.com'))"""
                )
            except Exception as exc:
                info["inspection_error"] = repr(exc)
            pages.append(info)

        await browser.close()

    out.write_text(json.dumps({
        "pages": pages,
        "responses": captured,
        "console": console,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(out),
        "pages": [{"url": x.get("url"), "status": x.get("status"), "final_url": x.get("final_url")} for x in pages],
        "responses": len(captured),
        "console": len(console),
    }, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
