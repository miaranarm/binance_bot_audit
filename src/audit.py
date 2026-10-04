import csv,re,os,json,datetime
from playwright.sync_api import sync_playwright
URL="https://www.binance.com/en/trading-bots";OUT="results";os.makedirs(OUT,exist_ok=True)
PAT=re.compile(r"([A-Z0-9]{1,20}(?:/)?(?:USDT|USDC|FDUSD|BTC|ETH))\s+(?:Perp\s+)?(.{1,100}?)\s+(?:Create\s+)?PNL\s+\(USD\)\s+([+-]?[\d,]+(?:\.\d+)?)\s+ROI\s+([+-]?[\d,]+(?:\.\d+)?)%\s+Runtime\s+(.{1,40}?)\s+Min\. Investment\s+([\d,.-]+)\s+\w+\s+24H/Total Matched Trades\s+([\d,]+)/([\d,]+)\s+7D MDD\s+([\d.]+)%",re.S)
def click(p,s):
 try:
  q=p.get_by_text(s,exact=True)
  if q.count(): q.last.click(timeout=4000);p.wait_for_timeout(900);return 1
 except: pass
 return 0
def scan(p,cat,sort):
 a=[]
 for m in PAT.finditer(p.locator("body").inner_text()):
  pair,meta,pnl,roi,run,inv,t24,tt,mdd=m.groups()
  a.append(dict(category=cat,sort=sort,pair=pair.replace("/",""),meta=meta.strip(),pnl_usd=float(pnl.replace(",","")),roi_pct=float(roi),runtime=run.strip(),min_investment=float(inv.replace(",","")),trades_24h=int(t24.replace(",","")),trades_total=int(tt.replace(",","")),mdd7d_pct=float(mdd)))
 return a
with sync_playwright() as w:
 b=w.chromium.launch(headless=True);p=b.new_page(viewport={"width":1440,"height":1200},locale="en-US");rows=[];dbg=[];api=[];reqs=[];charts=[]
 def req(r):
  try:
   if r.resource_type in ("xhr","fetch") and "binance.com/bapi/" in r.url and any(x in r.url for x in ("queryTopStrategy","queryTopUmDcaStrategy","queryRoiChart")):
    reqs.append({"url":r.url,"method":r.method,"post_data":r.post_data or ""})
  except Exception as e: dbg.append("REQ_ERROR "+str(e))
 def resp(r):
  try:
   if r.request.resource_type in ("xhr","fetch") and "binance.com/bapi/" in r.url:
    api.append(r.url)
    if any(x in r.url for x in ("queryTopStrategy","queryTopUmDcaStrategy","queryRoiChart")):
     body=r.body().decode("utf-8","replace")
     name=re.sub(r"[^A-Za-z0-9]+","_",r.url.split("/")[-1].split("?")[0])
     open(f"{OUT}/api_{name}_{len(api)}.json","w",encoding="utf-8").write(body)
     dbg.append("API "+r.url+" BODY "+str(len(body)))
  except Exception as e: dbg.append("RESP_ERROR "+str(e))
 p.on("request",req);p.on("response",resp)
 p.goto(URL,wait_until="domcontentloaded",timeout=60000);p.wait_for_timeout(7000)
 endpoint="https://www.binance.com/bapi/futures/v1/public/future/common/strategy/landing-page/queryTopStrategy"
 for page in range(1,30):
  body={"page":page,"rows":100,"strategyType":1,"symbol":"","zone":"","sort":"pnl"}
  r=p.request.post(endpoint,data=body);j=r.json();data=j.get("data") or []
  dbg.append(f"SPOT page={page} rows={len(data)} total={j.get('total')}")
  for x in data:
   x["snapshot_utc"]=datetime.datetime.now(datetime.timezone.utc).isoformat();x["category"]="Spot Grid";x["sort"]="pnl";rows.append(x)
  if len(data)<100 or page*100>=int(j.get("total") or 0): break
 for x in rows:
  sid=x.get("strategyId")
  if sid:
   try:
    r=p.request.post("https://www.binance.com/bapi/futures/v1/public/future/common/strategy/landing-page/queryRoiChart",data={"strategyId":sid,"streamerStrategyType":"SPOT_GRID"})
    z=r.json().get("data") or [];charts.append({"strategyId":sid,"data":z})
   except Exception as e: dbg.append(f"CHART_ERROR {sid} {e}")
 open(f"{OUT}/spot_all.json","w",encoding="utf-8").write(json.dumps(rows,ensure_ascii=False))
 open(f"{OUT}/spot_roi_charts.json","w",encoding="utf-8").write(json.dumps(charts,ensure_ascii=False))
 open(f"{OUT}/debug.txt","w").write("\n".join(dbg))
 open(f"{OUT}/api_urls.txt","w").write("\n".join(dict.fromkeys(api)))
 open(f"{OUT}/api_requests.json","w",encoding="utf-8").write(json.dumps(reqs,ensure_ascii=False,indent=2))
 b.close()
fields=sorted({k for x in rows for k in x})
with open(f"{OUT}/bots.csv","w",newline="",encoding="utf-8") as f:
 w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore");w.writeheader();w.writerows(rows)
print(f"OK: {len(rows)} bots, {len(charts)} ROI charts")
