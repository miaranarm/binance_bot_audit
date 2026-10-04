import csv,re,os,datetime
from playwright.sync_api import sync_playwright
URL="https://www.binance.com/en/trading-bots"; OUT="results"; os.makedirs(OUT,exist_ok=True)
PAT=re.compile(r"([A-Z0-9]{1,20}(?:/)?(?:USDT|USDC|FDUSD|BTC|ETH))\s+(?:Perp\s+)?(.{1,100}?)\s+(?:Create\s+)?PNL\s+\(USD\)\s+([+-]?[\d,]+(?:\.\d+)?)\s+ROI\s+([+-]?[\d,]+(?:\.\d+)?)%\s+Runtime\s+(.{1,40}?)\s+Min\. Investment\s+([\d,.-]+)\s+\w+\s+24H/Total Matched Trades\s+([\d,]+)/([\d,]+)\s+7D MDD\s+([\d.]+)%",re.S)
def click(p,s):
 for role in ("tab","button"):
  try:
   q=p.get_by_role(role,name=s,exact=True)
   if q.count(): q.first.click(timeout=3000);p.wait_for_timeout(900);return 1
  except: pass
 return 0
def scan(p,cat,sort):
 a=[]
 for n in range(50):
  for m in PAT.finditer(p.locator("body").inner_text()):
   pair,meta,pnl,roi,run,inv,t24,tt,mdd=m.groups()
   a.append(dict(category=cat,sort=sort,pair=pair.replace("/",""),meta=meta.strip(),pnl_usd=float(pnl.replace(",","")),roi_pct=float(roi),runtime=run.strip(),min_investment=float(inv.replace(",","")),trades_24h=int(t24.replace(",","")),trades_total=int(tt.replace(",","")),mdd7d_pct=float(mdd)))
  if not click(p,str(n+2)): break
 return a
with sync_playwright() as w:
 b=w.chromium.launch(headless=True);p=b.new_page(viewport={"width":1440,"height":1200},locale="en-US");p.goto(URL,wait_until="domcontentloaded",timeout=60000);p.wait_for_timeout(9000)
 rows=[];dbg=[]
 for cat in ["Spot Grid","Futures Grid","Futures DCA","Arbitrage"]:
  ok=click(p,cat);dbg.append(f"{cat} {ok} {p.url}")
  if cat not in p.locator("body").inner_text():continue
  for sort in ["Top PNL","Top ROI","Most Copied","Most Matched"]:
   ok=click(p,sort);dbg.append(f"{cat}/{sort} {ok} {p.url}");rows+=scan(p,cat,sort)
 open(f"{OUT}/debug.txt","w").write("\n".join(dbg));open(f"{OUT}/rendered.txt","w").write(p.locator("body").inner_text());b.close()
u={(x["category"],x["pair"],x["meta"],x["pnl_usd"],x["roi_pct"],x["runtime"],x["trades_total"]):x for x in rows};rows=list(u.values())
def day(s):
 d=re.search(r"(\d+)d",s);h=re.search(r"(\d+)h",s);return (int(d.group(1)) if d else 0)+(int(h.group(1)) if h else 0)/24
for x in rows:
 x["runtime_days"]=round(day(x["runtime"]),2);x["roi_mdd"]=round(x["roi_pct"]/max(x["mdd7d_pct"],.1),4);x["roi_per_day"]=round(x["roi_pct"]/max(x["runtime_days"],.25),4);x["maturity"]="Emerging" if x["runtime_days"]<7 else ("Developing" if x["runtime_days"]<30 else "Mature");x["risk_class"]="Reject" if x["mdd7d_pct"]>40 else ("Aggressive" if x["mdd7d_pct"]>25 else "Candidate")
# Recent score: 75% current, 25% prior snapshots once history exists.
hist={};hp=f"{OUT}/history.csv"
if os.path.exists(hp):
 with open(hp,encoding="utf-8") as f:
  for r in csv.DictReader(f):hist.setdefault((r["category"],r["pair"],r["meta"]),[]).append(r)
for x in rows:
 h=hist.get((x["category"],x["pair"],x["meta"]),[])[-5:];q=sum(float(r["roi_mdd"]) for r in h)/len(h) if h else x["roi_mdd"];x["recent_score"]=round(.75*x["roi_mdd"]+.25*q,4)
 x["snapshot_utc"]=datetime.datetime.now(datetime.timezone.utc).isoformat()
fields=["snapshot_utc","category","sort","pair","meta","pnl_usd","roi_pct","runtime","runtime_days","min_investment","trades_24h","trades_total","mdd7d_pct","roi_per_day","roi_mdd","recent_score","maturity","risk_class"]
with open(hp,"a",newline="",encoding="utf-8") as f:
 w=csv.DictWriter(f,fieldnames=fields)
 if f.tell()==0:w.writeheader()
 w.writerows(rows)
rows.sort(key=lambda x:(x["risk_class"]!="Candidate",-x["recent_score"],-x["pnl_usd"]))
with open(f"{OUT}/bots.csv","w",newline="",encoding="utf-8") as f:
 w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
with open(f"{OUT}/top3.md","w",encoding="utf-8") as f:
 f.write("# Recent-weighted Binance Bot Audit\n\n")
 for i,x in enumerate(rows[:15],1):f.write("## %d. %s — %s %s\nScore %.3f | ROI %.2f%% | PNL $%s | MDD %.2f%% | Runtime %s | 24h/total %s/%s | %s\n"%(i,x["category"],x["pair"],x["meta"],x["recent_score"],x["roi_pct"],format(x["pnl_usd"],",.2f"),x["mdd7d_pct"],x["runtime"],format(x["trades_24h"],","),format(x["trades_total"],","),x["risk_class"]))