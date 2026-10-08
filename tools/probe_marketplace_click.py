import asyncio, json
from pathlib import Path
from playwright.async_api import async_playwright

URL="https://www.binance.com/en/trading-bots/spot/grid/XRPBTC"

async def main():
    out=Path("results/marketplace_click_raw.json"); out.parent.mkdir(parents=True,exist_ok=True)
    captured=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=["--disable-blink-features=AutomationControlled","--no-sandbox"])
        context=await browser.new_context(locale="en-US",timezone_id="Africa/Nairobi")
        page=await context.new_page()
        async def resp(r):
            u=r.url
            if "binance.com" not in u: return
            try:
                b=await r.text()
            except Exception: b=""
            if r.request.resource_type in ("xhr","fetch") or any(k in (u+b).lower() for k in ("strategy","algo","grid","profit","pnl")):
                captured.append({"url":u,"status":r.status,"method":r.request.method,"resource_type":r.request.resource_type,"request_body":r.request.post_data,"body":b[:300000]})
        page.on("response",resp)
        nav={}
        try:
            r=await page.goto(URL,wait_until="domcontentloaded",timeout=90000)
            nav={"status":r.status if r else None,"url":page.url}
        except Exception as e: nav={"error":repr(e)}
        await page.wait_for_timeout(12000)
        targets=await page.get_by_text("PUMP/USDT",exact=True).all()
        target_info=[]
        for i,t in enumerate(targets[:5]):
            try:
                target_info.append({"i":i,"tag":await t.evaluate("(e)=>e.tagName"),"html":(await t.evaluate("(e)=>e.parentElement?.parentElement?.outerHTML||e.outerHTML"))[:10000]})
            except Exception as e: target_info.append({"i":i,"error":repr(e)})
        clicked=False
        if targets:
            try:
                await targets[0].click(timeout=10000)
                clicked=True
            except Exception as e: target_info.append({"click_error":repr(e)})
        await page.wait_for_timeout(15000)
        state={"url":page.url,"title":await page.title(),"text":(await page.locator("body").inner_text())[:15000]}
        out.write_text(json.dumps({"navigation":nav,"targets":target_info,"clicked":clicked,"state":state,"responses":captured},ensure_ascii=False,indent=2),encoding="utf-8")
        await browser.close()
    print(json.dumps({"output":str(out),"navigation":nav,"clicked":clicked,"final_url":state.get("url"),"responses":len(captured)},ensure_ascii=False))
if __name__=="__main__": asyncio.run(main())
