import asyncio,json
from pathlib import Path
from playwright.async_api import async_playwright

URL="https://www.binance.com/en/trading-bots/spot/grid/XRPBTC"
SID="3232564"
PATHS=[
"/bapi/futures/v1/private/strategy/grid/query-grid-matched-items",
"/bapi/futures/v1/friendly/strategy/grid/query-grid-matched-items",
"/bapi/futures/v1/public/strategy/grid/query-grid-matched-items",
"/bapi/futures/v1/private/strategy/grid/query-grid-commissions",
"/bapi/futures/v1/friendly/strategy/grid/query-grid-commissions",
"/bapi/futures/v1/public/strategy/grid/query-grid-commissions",
"/bapi/algo/v1/friendly/algo/public/strategy/detail",
"/bapi/algo/v1/public/algo/public/strategy/detail",
]
async def main():
 out=Path("results/matched_endpoint_variants.json"); out.parent.mkdir(parents=True,exist_ok=True)
 async with async_playwright() as p:
  b=await p.chromium.launch(headless=True,args=["--disable-blink-features=AutomationControlled","--no-sandbox"])
  c=await b.new_context(locale="en-US",timezone_id="Africa/Nairobi",user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")
  page=await c.new_page()
  await page.goto(URL,wait_until="domcontentloaded",timeout=90000)
  await page.wait_for_timeout(12000)
  results=[]
  for path in PATHS:
   if "algo/public/strategy/detail" in path:
    method="POST"; body={"strategyId":SID}; url="https://www.binance.com"+path
   else:
    method="GET"; body=None; url="https://www.binance.com"+path+"?strategyId="+SID+"&page=1&size=20"
   js="""async ({url,method,body})=>{try{const r=await fetch(url,{method,credentials:'include',headers:{'content-type':'application/json','clienttype':'web','lang':'en'},body:body?JSON.stringify(body):undefined});return {status:r.status,statusText:r.statusText,ct:r.headers.get('content-type'),text:(await r.text()).slice(0,100000)}}catch(e){return {error:String(e)}}}"""
   res=await page.evaluate(js,{"url":url,"method":method,"body":body})
   results.append({"path":path,"method":method,"url":url,"result":res})
  out.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf-8")
  await b.close()
 print(json.dumps({"output":str(out),"count":len(results)},ensure_ascii=False))
if __name__=="__main__": asyncio.run(main())
