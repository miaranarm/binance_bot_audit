import csv,datetime,os
from playwright.sync_api import sync_playwright
OUT="results";os.makedirs(OUT,exist_ok=True)
BASE="https://www.binance.com/bapi/futures/v1/public/future/common/strategy/landing-page/"
TOP=BASE+"queryTopStrategy";DCA=BASE+"queryTopUmDcaStrategy";CH=BASE+"queryRoiChart";CUT=182*86400
def num(v):
 try:return float(v)
 except:return 0
def lev(x):
 p=x.get("strategyParams") or {};v=p.get("leverage",x.get("leverage"))
 if v in (None,"","null"):return 1
 try:return float(v)
 except:return 99
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
def pages(p,ep,base,cat,stream):
 out=[]
 for page in range(1,150):
  q=dict(base);q.update(page=page,rows=100)
  try:j=p.request.post(ep,data=q).json()
  except:break
  d=j.get("data") or []
  for x in d:
   x=dict(x);x.update(category=cat,streamer=stream,leverage_detected=lev(x));out.append(x)
  if len(d)<100 or page*100>=int(j.get("total") or 0):break
 return out
with sync_playwright() as w:
 b=w.chromium.launch(headless=True);p=b.new_page();p.goto("https://www.binance.com/en/trading-bots",wait_until="domcontentloaded",timeout=60000);p.wait_for_timeout(5000)
 rows=pages(p,TOP,{"strategyType":1,"symbol":"","zone":"","sort":"pnl"},"Spot Grid","SPOT_GRID")
 for st in range(2,13):rows+=pages(p,TOP,{"strategyType":st,"symbol":"","zone":"","sort":"pnl"},"StrategyType "+str(st),"UNKNOWN")
 rows+=pages(p,DCA,{"market":"","zone":"","roi":"","sort":"pnl","trailingType":"","leverage":"","investmentType":False,"sevenDayMdd":"","strategyType":10,"symbol":""},"Futures DCA","UM_DCA")
 rows=[x for x in rows if lev(x)<=1 and num(x.get("runningTime"))>=CUT]
 seen=set();rows=[x for x in rows if not (x.get("strategyId") in seen or seen.add(x.get("strategyId")))]
 hist=[]
 for i,x in enumerate(rows,1):
  try:
   z=p.request.post(CH,data={"strategyId":x.get("strategyId"),"streamerStrategyType":x.get("streamer")}).json().get("data") or [];pts=[];walk(z,pts)
   r=dict(strategyId=x.get("strategyId"),category=x.get("category"),symbol=x.get("symbol"),roi_now=x.get("roi"),runningTime=x.get("runningTime"),leverage=x.get("leverage_detected"),roi_2026_04_01="",roi_2026_09_30="",date_2026_04_01="",date_2026_09_30="")
   for d,k in [(datetime.datetime(2026,4,1,tzinfo=datetime.timezone.utc),"2025_10_01"),(datetime.datetime(2026,9,30,tzinfo=datetime.timezone.utc),"2026_09_30")]:
    if pts:
     t,v=min(pts,key=lambda q:abs((q[0]-d).total_seconds()))
     if abs((t-d).days)<=7:r["roi_"+k]=v;r["date_"+k]=t.isoformat()
   r["gain_100_usdt"]=round(float(r["roi_2026_09_30"])-float(r["roi_2025_10_01"]),8) if r["roi_2025_10_01"]!="" and r["roi_2026_09_30"]!="" else ""
   hist.append(r)
  except:pass
  if i%100==0:print("HIST",i)
 hist.sort(key=lambda x:(x["gain_100_usdt"]=="",-(x["gain_100_usdt"] or 0)))
 with open(f"{OUT}/all_no_leverage_survivors.csv","w",newline="",encoding="utf-8") as f:
  w=csv.DictWriter(f,fieldnames=hist[0].keys() if hist else ["strategyId"]);w.writeheader();w.writerows(hist)
 with open(f"{OUT}/all_no_leverage_raw.csv","w",newline="",encoding="utf-8") as f:
  fields=sorted({k for x in rows for k in x});w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore");w.writeheader();w.writerows(rows)
 open(f"{OUT}/summary.txt","w").write(f"SURVIVORS={len(rows)}\nWITH_HIST={sum(x['gain_100_usdt']!='' for x in hist)}")
 b.close()
print("DONE",len(rows),len(hist))