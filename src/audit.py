import csv,re,os,datetime
from playwright.sync_api import sync_playwright
URL="https://www.binance.com/en/trading-bots";OUT="results";os.makedirs(OUT,exist_ok=True)
PAT=re.compile(r"([A-Z0-9]{1,20}(?:/)?(?:USDT|USDC|FDUSD|BTC|ETH))\s+(?:Perp\s+)?(.{1,100}?)\s+(?:Create\s+)?PNL\s+\(USD\)\s+([+-]?[\d,]+(?:\.\d+)?)\s+ROI\s+([+-]?[\d,]+(?:\.\d+)?)%\s+Runtime\s+(.{1,40}?)\s+Min\. Investment\s+([\d,.-]+)\s+\w+\s+24H/Total Matched Trades\s+([\d,]+)/([\d,]+)\s+7D MDD\s+([\d.]+)%",re.S)
def click(p,s):
 try:
  q=p.get_by_text(s,exact=True)
  if q.count(): q.last.click(timeout=4000);p.wait_for_timeout(900);return 1
 except:pass
 return 0
def scan(p,cat,sort):
 a=[]
 for m in PAT.finditer(p.locator("body").inner_text()):
  pair,meta,pnl,roi,run,inv,t24,tt,mdd=m.groups()
  a.append(dict(category=cat,sort=sort,pair=pair.replace("/",""),meta=meta.strip(),pnl_usd=float(pnl.replace(",","")),roi_pct=float(roi),runtime=run.strip(),min_investment=float(inv.replace(",","")),trades_24h=int(t24.replace(",","")),trades_total=int(tt.replace(",","")),mdd7d_pct=float(mdd)))
 return a
with sync_playwright() as w:
 b=w.chromium.launch(headless=True);p=b.new_page(viewport={"width":1440,"height":1200},locale="en-US");p.goto(URL,wait_until="domcontentloaded",timeout=60000);p.wait_for_timeout(8000)
 rows=[];dbg=[]
 api_urls=[]
 def resp(r):
  try:
   if r.request.resource_type in ("xhr","fetch") and "binance.com" in r.url: api_urls.append(r.url)
  except: pass
 p.on("response",resp)
 cats=["Spot Grid","Futures Grid","Futures DCA","Arbitrage"]
 sorts=["Top PNL","Top ROI","Most Copied","Most Matched"]
 for cat in cats:
  ok=click(p,cat);dbg.append(f"{cat} click={ok}")
  for sort in (["3d APR","7d APR","30d APR","Next Available"] if cat=="Arbitrage" else sorts):
   ok=click(p,sort);dbg.append(f"{cat}/{sort} click={ok}")
   z=scan(p,cat,sort);dbg.append(f"rows={len(z)} first={z[0]['pair'] if z else '-'}");rows+=z
 open(f"{OUT}/debug.txt","w").write("\n".join(dbg));open(f"{OUT}/rendered.txt","w").write(p.locator("body").inner_text());open(f"{OUT}/api_urls.txt","w").write("\n".join(dict.fromkeys(api_urls)));b.close()
fields=["snapshot_utc","category","sort","pair","meta","pnl_usd","roi_pct","runtime","min_investment","trades_24h","trades_total","mdd7d_pct"]
with open(f"{OUT}/bots.csv","w",newline="",encoding="utf-8") as f:
 w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
