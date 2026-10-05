import csv,datetime,os,json
from playwright.sync_api import sync_playwright
OUT="results";os.makedirs(OUT,exist_ok=True)
BASE="https://www.binance.com/bapi/futures/v1/public/future/common/strategy/landing-page/"
TOP=BASE+"queryTopStrategy";DCA=BASE+"queryTopUmDcaStrategy";CH=BASE+"queryRoiChart"
DATES=[datetime.datetime(2025,10,1,tzinfo=datetime.timezone.utc),datetime.datetime(2026,4,1,tzinfo=datetime.timezone.utc),datetime.datetime(2026,9,1,tzinfo=datetime.timezone.utc),datetime.datetime(2026,9,30,tzinfo=datetime.timezone.utc)]
def f(v):
 try:return float(v)
 except:return None
def lev(x):
 p=x.get("strategyParams") or {};v=p.get("leverage",x.get("leverage"))
 if v in (None,"","null"):return 1.0
 try:return float(v)
 except:return 99.0
def ts(v):
 try:
  if isinstance(v,(int,float)):return datetime.datetime.fromtimestamp(v/1000 if v>1e11 else v,tz=datetime.timezone.utc)
  return datetime.datetime.fromisoformat(str(v).replace("Z","+00:00")).astimezone(datetime.timezone.utc)
 except:return None
def walk(x,out):
 if isinstance(x,dict):
  t=next((ts(x[k]) for k in x if str(k).lower() in ("time","timestamp","date","datetime","createtime","updatetime","x") and ts(x[k])),None)
  if t:
   for k,v in x.items():
    if str(k).lower() in ("roi","roi_pct","value","y","return"):
     try:out.append((t,float(str(v).replace("%",""))));break
     except:pass
  for v in x.values():walk(v,out)
 elif isinstance(x,list):
  for v in x:walk(v,out)
def pages(p,ep,base,cat,stream,diag):
 out=[]
 for page in range(1,150):
  q=dict(base);q.update(page=page,rows=100)
  try:z=p.request.post(ep,data=q).json();d=z.get("data") or []
  except Exception as e:
   if page==1:diag.append({"endpoint":ep,"category":cat,"error":str(e)})
   break
  if page==1:diag.append({"endpoint":ep,"category":cat,"total":z.get("total"),"first_strategyType":d[0].get("strategyType") if d else None})
  for x in d:
   x=dict(x);x["_category"]=cat;x["_streamer"]=stream;x["_lev"]=lev(x);out.append(x)
  if len(d)<100 or page*100>=int(z.get("total") or 0):break
 return out
with sync_playwright() as w:
 b=w.chromium.launch(headless=True);p=b.new_page();net=set();diag=[]
 p.on("request",lambda r:net.add(r.url) if "/bapi/" in r.url else None)
 for u in ["https://www.binance.com/en/trading-bots","https://www.binance.com/en/trading-bots/spot/grid/DOGEUSDT","https://www.binance.com/en/trading-bots/spot/dca-bot/BTCUSDT","https://www.binance.com/en/trading-bots/spot/rebalancing-bot/BTCUSDT","https://www.binance.com/en/trading-bots/futures/arbitrage/BTCUSDT","https://www.binance.com/en/trading-bots/futures/dca-bot/BTCUSDT","https://www.binance.com/en/trading-bots/futures/snowball/BTCUSDT"]:
  try:p.goto(u,wait_until="domcontentloaded",timeout=30000);p.wait_for_timeout(3000)
  except:pass
 open(f"{OUT}/discovered_bapi_endpoints.txt","w").write("\n".join(sorted(net)))
 rows=pages(p,TOP,{"strategyType":1,"symbol":"","zone":"","sort":"pnl"},"Spot Grid","SPOT_GRID",diag)
 for st in range(2,21):rows+=pages(p,TOP,{"strategyType":st,"symbol":"","zone":"","sort":"pnl"},"Marketplace type "+str(st),"TYPE_"+str(st),diag)
 rows+=pages(p,DCA,{"market":"","zone":"","roi":"","sort":"pnl","trailingType":"","leverage":"","investmentType":False,"sevenDayMdd":"","strategyType":10,"symbol":""},"Futures DCA","UM_DCA",diag)
 TARGET_STRATEGY_ID="8799020"
 TARGET_STREAMER="SPOT_GRID"
 target_result={"strategyId":TARGET_STRATEGY_ID,"streamerStrategyType":TARGET_STREAMER,"retrieved_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),"points":[],"status":"unknown"}
 try:
  tz=p.request.post(CH,data={"strategyId":TARGET_STRATEGY_ID,"streamerStrategyType":TARGET_STREAMER}).json()
  target_result["raw_data"]=tz.get("data") or []
  pts=[];walk(target_result["raw_data"],pts)
  pts=sorted(set(pts),key=lambda q:q[0])
  target_result["points"]=[{"time":t.isoformat(),"roi":v} for t,v in pts]
  best=None;window=datetime.timedelta(days=7)
  for i,(t,v) in enumerate(pts):
   peak_t,peak_roi=max(((tp,vp) for tp,vp in pts[:i+1] if t-tp<=window),key=lambda q:q[1],default=(None,None))
   if peak_roi is None or peak_roi<=-100:continue
   draw=(peak_roi-v)/(100.0+peak_roi)*100.0
   if best is None or draw>best["mdd7d_pct"]:
    best={"mdd7d_pct":draw,"peak_time":peak_t.isoformat(),"peak_roi":peak_roi,"trough_time":t.isoformat(),"trough_roi":v,"window_days":7}
  target_result["computed_mdd7d"]=best;target_result["status"]="ok"
 except Exception as e:
  target_result["status"]="error";target_result["error"]=str(e)
 with open(f"{OUT}/target_8799020_roi.json","w",encoding="utf-8") as z:json.dump(target_result,z,ensure_ascii=False,indent=2)
 with open(f"{OUT}/target_8799020_mdd7d.txt","w",encoding="utf-8") as z:z.write(json.dumps(target_result.get("computed_mdd7d"),ensure_ascii=False,indent=2))
 now=datetime.datetime.now(datetime.timezone.utc)
 for x in rows:
  x["_start"]=(now-datetime.timedelta(seconds=f(x.get("runningTime")) or 0)).isoformat()
  for k,d in zip(("_p101","_p401","_p901","_p930"),DATES):x[k]=int(x["_start"][:10]<=d.strftime("%Y-%m-%d"))
 keep=[x for x in rows if x["_lev"]<=1 and all(x[k] for k in ("_p101","_p401","_p901","_p930"))]
 seen=set();keep=[x for x in keep if not(x.get("strategyId") in seen or seen.add(x.get("strategyId")))]
 hist=[]
 for x in keep:
  try:
   z=p.request.post(CH,data={"strategyId":x.get("strategyId"),"streamerStrategyType":x.get("_streamer")}).json().get("data") or []
   pts=[];walk(z,pts)
   r={"strategyId":x.get("strategyId"),"category":x.get("_category"),"strategyType":x.get("strategyType"),"symbol":x.get("symbol"),"leverage":x.get("_lev"),"runningTime":x.get("runningTime"),"estimated_start":x.get("_start")}
   for d,label in zip(DATES,["2025_10_01","2026_04_01","2026_09_01","2026_09_30"]):
    r["roi_"+label]="";r["date_"+label]=""
    if pts:
     t,v=min(pts,key=lambda q:abs((q[0]-d).total_seconds()))
     if abs((t-d).total_seconds())<=7*86400:r["roi_"+label]=v;r["date_"+label]=t.isoformat()
   hist.append(r)
  except:pass
def n(x,ks):
 for k in ks:
  try:return float(x.get(k))
  except:pass
 return 0.0
def score(x,a):
 cfg=[(["roi","roiPct"],.30,1),(["pnl","profitLoss","totalPnl"],.25,1),(["runningTime"],.15,1),(["matchedTrades","matchedCount","totalMatchedTrades"],.15,1),(["mdd7d","sevenDayMdd","7dMdd"],.15,-1)]
 s=0
 for ks,w,d in cfg:
  z=[n(y,ks) for y in a];v=n(x,ks);lo,hi=min(z),max(z);q=(v-lo)/(hi-lo) if hi>lo else .5
  s+=w*q*d
 return 100*s
cur=[]
for x in rows:
 if x["_lev"]<=1:
  y={"strategyId":x.get("strategyId"),"category":x.get("_category"),"strategyType":x.get("strategyType"),"symbol":x.get("symbol"),"leverage":x.get("_lev"),"minInvestment":x.get("minInvestment",x.get("minimumInvestment","")),"runningTime":x.get("runningTime"),"roi":n(x,["roi","roiPct","roiRate"]),"pnl":n(x,["pnl","profitLoss","totalPnl"]),"matchedTrades":n(x,["matchedTrades","matchedCount","totalMatchedTrades"]),"mdd7d":n(x,["mdd7d","sevenDayMdd","7dMdd"])}

  cur.append(y)
cur=[x for x in cur if x["roi"] or x["pnl"] or x["matchedTrades"]]
for x in cur:x["score"]=score(x,cur)
cur.sort(key=lambda x:x["score"],reverse=True)
with open(f"{OUT}/current_multicriteria.csv","w",newline="",encoding="utf-8") as z:
 w=csv.DictWriter(z,fieldnames=["rank","strategyId","category","strategyType","symbol","leverage","minInvestment","runningTime","roi","pnl","matchedTrades","mdd7d","score"]);w.writeheader()
 for i,x in enumerate(cur,1):w.writerow({"rank":i,**x})
with open(f"{OUT}/current_multicriteria_summary.txt","w") as z:
 z.write(f"COUNT={len(cur)}\nSCORING=ROI30 PNL25 RUNTIME15 MATCHED15 LOW_7D_MDD15\n")
 for i,x in enumerate(cur[:20],1):z.write(f"{i}. {x['strategyId']} {x['symbol']} score={x['score']:.2f} roi={x['roi']} pnl={x['pnl']} trades={x['matchedTrades']} mdd7d={x['mdd7d']}\n")
 hist.sort(key=lambda x:(x["roi_2026_09_30"]=="",-float(x["roi_2026_09_30"] or 0)))
 with open(f"{OUT}/all_no_leverage_4date.csv","w",newline="",encoding="utf-8") as z:
  fields=list(hist[0]) if hist else ["strategyId"];w=csv.DictWriter(z,fieldnames=fields);w.writeheader();w.writerows(hist)
 with open(f"{OUT}/type_census.json","w",encoding="utf-8") as z:json.dump({"official_types":["Spot Grid","Auto-Invest","Spot DCA","Rebalancing Bot","Futures Grid","Futures DCA","Position Snowball","Futures TWAP","Spot Algo","Futures VP","Funding Rate Arbitrage"],"endpoint_diagnostics":diag},z,ensure_ascii=False,indent=2)
 with open(f"{OUT}/summary.txt","w") as z:
  z.write(f"RAW={len(rows)}\nNO_LEVERAGE_4_DATES={len(keep)}\nWITH_ROI_04_09={sum(x['roi_2026_09_30']!='' for x in hist)}\nSORT=ROI_2026_09_30_DESC\n")
  z.write("NOTE=Presence is inferred from current public strategy runtime; Binance public marketplace does not expose a historical daily snapshot for every bot family.\n")
 with open(f"{OUT}/all_no_leverage_raw.csv","w",newline="",encoding="utf-8") as z:
  fields=sorted({k for x in rows for k in x});w=csv.DictWriter(z,fieldnames=fields,extrasaction="ignore");w.writeheader();w.writerows(rows)
print("DONE",len(rows),len(keep),len(hist))