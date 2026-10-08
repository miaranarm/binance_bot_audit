import asyncio, json, re
from pathlib import Path
from playwright.async_api import async_playwright

URL="https://www.binance.com/en/trading-bots/spot/grid/XRPBTC"
TERMS=("strategy/detail","gridProfit","totalProfit","floatingProfit","floatingPnl","matchedProfit","profitPerGrid","queryTopStrategy","queryRoiChart")

async def main():
    out=Path("results/marketplace_js_endpoints.json"); out.parent.mkdir(parents=True,exist_ok=True)
    hits=[]
    scripts=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=["--disable-blink-features=AutomationControlled","--no-sandbox"])
        context=await browser.new_context(locale="en-US",timezone_id="Africa/Nairobi")
        page=await context.new_page()
        async def resp(r):
            if r.request.resource_type!="script": return
            u=r.url
            try: b=await r.text()
            except Exception: return
            scripts.append({"url":u,"size":len(b)})
            low=b.lower()
            found=[t for t in TERMS if t.lower() in low]
            if not found: return
            snippets=[]
            for t in found:
                for m in list(re.finditer(re.escape(t),b,re.I))[:10]:
                    snippets.append({"term":t,"snippet":b[max(0,m.start()-800):m.end()+1600]})
            hits.append({"url":u,"size":len(b),"terms":found,"snippets":snippets[:50]})
        page.on("response",resp)
        nav={}
        try:
            r=await page.goto(URL,wait_until="domcontentloaded",timeout=90000)
            nav={"status":r.status if r else None,"url":page.url}
        except Exception as e: nav={"error":repr(e)}
        await page.wait_for_timeout(30000)
        resources=await page.evaluate("""() => performance.getEntriesByType('resource').filter(e=>e.initiatorType==='script').map(e=>e.name)""")
        result={"navigation":nav,"script_count":len(scripts),"scripts":scripts,"hits":hits,"performance_scripts":resources}
        out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        await browser.close()
    print(json.dumps({"output":str(out),"scripts":len(scripts),"hits":len(hits)},ensure_ascii=False))
if __name__=="__main__": asyncio.run(main())
