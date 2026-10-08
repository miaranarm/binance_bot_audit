import asyncio, json, re
from pathlib import Path
from playwright.async_api import async_playwright

URL="https://www.binance.com/en/trading-bots/spot/grid/XRPBTC"
TERMS=("strategy/detail","gridProfit","totalProfit","floatingProfit","floatingPnl","matchedProfit","profitPerGrid","queryTopStrategy","queryRoiChart","SpotGridMatchedInfo","SpotGridMatchedList","SPOT_GRID_MATCHED_INFO","SPOT_GRID_MATCHED_LIST","matched-info","matched-list")

async def main():
    out=Path("results/marketplace_js_endpoints.json"); out.parent.mkdir(parents=True,exist_ok=True)
    hits=[]; scripts=[]; full_sources={}
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
                for m in list(re.finditer(re.escape(t),b,re.I))[:20]:
                    snippets.append({"term":t,"snippet":b[max(0,m.start()-1500):m.end()+5000]})
            hits.append({"url":u,"size":len(b),"terms":found,"snippets":snippets[:120]})
            if any(t.lower() in low for t in ("spotgridmatchedinfo","spotgridmatchedlist","spot_grid_matched_info","spot_grid_matched_list","gridprofit","totalprofit","floatingprofit","matchedprofit","strategy/detail")):
                key=re.sub(r"[^A-Za-z0-9]+","_",u.rsplit("/",1)[-1])[:80]
                full_sources[key]=b
        page.on("response",resp)
        nav={}
        try:
            r=await page.goto(URL,wait_until="domcontentloaded",timeout=90000)
            nav={"status":r.status if r else None,"url":page.url}
        except Exception as e: nav={"error":repr(e)}
        await page.wait_for_timeout(30000)
        resources=await page.evaluate("""() => performance.getEntriesByType('resource').filter(e=>e.initiatorType==='script').map(e=>e.name)""")
        out.write_text(json.dumps({"navigation":nav,"script_count":len(scripts),"scripts":scripts,"hits":hits,"performance_scripts":resources},ensure_ascii=False,indent=2),encoding="utf-8")
        for key,src in full_sources.items():
            Path(f"results/marketplace_js_hit_{key}.js").write_text(src,encoding="utf-8")
        await browser.close()
    print(json.dumps({"output":str(out),"scripts":len(scripts),"hits":len(hits),"full_sources":list(full_sources)},ensure_ascii=False))
if __name__=="__main__": asyncio.run(main())
