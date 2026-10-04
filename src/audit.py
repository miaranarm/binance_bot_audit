import csv,re,os
from playwright.sync_api import sync_playwright
URL="https://www.binance.com/en/trading-bots"; OUT="results"; os.makedirs(OUT,exist_ok=True)
PAT=re.compile(r"([A-Z0-9]{2,20}(?:/)?(?:USDT|USDC|FDUSD|BTC|ETH))\s+(?:Perp\s+)?(.{1,100}?)\s+(?:Create\s+)?PNL\s+\(USD\)\s+([+-]?[\d,]+(?:\.\d+)?)\s+ROI\s+([+-]?[\d,]+(?:\.\d+)?)%\s+Runtime\s+(.{1,40}?)\s+Min\. Investment\s+([\d,]+(?:\.\d+)?)\s+\w+\s+24H/Total Matched Trades\s+([\d,]+)/([\d,]+)\s+7D MDD\s+([\d.]+)%",re.S)
def click(pg,s):
 try:
  q=pg.get_by_text(s,exact=True); n=q.count()
  if n: q.nth(n-1).click(timeout=3000); pg.wait_for_timeout(1000); return True
 except: pass
 return False
def scan(pg,cat,sort):
 out=[]
 for page in range(1,51):
  body=pg.locator("body").inner_text()
  for m in PAT.finditer(body):
   pair,meta,pnl,roi,run,inv,t24,total,mdd=m.groups()
   out.append({"category":cat,"sort":sort,"pair":pair,"meta":meta.strip(),"pnl_usd":float(pnl.replace(",","")),"roi_pct":float(roi),"runtime":run.strip(),"min_investment":float(inv.replace(",","")),"trades_24h":int(t24.replace(",","")),"trades_total":int(total.replace(",","")),"mdd7d_pct":float(mdd)})
  if page==50 or not click(pg,str(page+1)): break
 return out
with sync_playwright() as p:
 b=p.chromium.launch(headless=True); pg=b.new_page(viewport={"width":1440,"height":1200},locale="en-US")
 pg.goto(URL,wait_until="domcontentloaded",timeout=60000); pg.wait_for_timeout(12000)
 rows=[]
 for cat in ["Spot Grid","Futures Grid","Futures DCA","Arbitrage"]:
  if click(pg,cat):
   for sort in ["Top PNL","Top ROI","Most Copied","Most Matched"]:
    click(pg,sort); rows += scan(pg,cat,sort)
 open(f"{OUT}/rendered.txt","w",encoding="utf-8").write(pg.locator("body").inner_text()); b.close()
u={}
for x in rows: u[(x["category"],x["pair"],x["meta"],x["pnl_usd"],x["roi_pct"],x["runtime"],x["trades_total"])]=x
rows=list(u.values())
def days(s):
 d=re.search(r"(\d+)d",s); h=re.search(r"(\d+)h",s)
 return (int(d.group(1)) if d else 0)+(int(h.group(1)) if h else 0)/24
for x in rows:
 x["runtime_days"]=round(days(x["runtime"]),2); x["roi_per_day"]=round(x["roi_pct"]/max(x["runtime_days"],.25),4); x["roi_mdd"]=round(x["roi_pct"]/max(x["mdd7d_pct"],.1),4)
 x["maturity"]="Emerging" if x["runtime_days"]<7 else ("Developing" if x["runtime_days"]<30 else "Mature")
 x["risk_class"]="Reject" if x["mdd7d_pct"]>40 else ("Aggressive" if x["mdd7d_pct"]>25 else "Candidate")
fields=["category","sort","pair","meta","pnl_usd","roi_pct","runtime","runtime_days","min_investment","trades_24h","trades_total","mdd7d_pct","roi_per_day","roi_mdd","maturity","risk_class"]
rows.sort(key=lambda x:(x["risk_class"]!="Candidate",-x["roi_mdd"],-x["pnl_usd"]))
with open(f"{OUT}/bots.csv","w",newline="",encoding="utf-8") as f: csv.DictWriter(f,fieldnames=fields).writeheader(); csv.DictWriter(f,fieldnames=fields).writerows(rows)
with open(f"{OUT}/top3.md","w",encoding="utf-8") as f:
 f.write("# Refined Binance Public Bot Audit\n\n")
 for i,x in enumerate(rows[:10],1): f.write(f"## {i}. {x["category"]} — {x["pair"]} {x["meta"]}\nROI {x["roi_pct"]:.2f}% | PNL ${x["pnl_usd"]:,.2f} | MDD {x["mdd7d_pct"]:.2f}% | Runtime {x["runtime"]} | Trades {x["trades_total"]:,} | {x["risk_class"]}\n")
print("Collected raw",len(rows))