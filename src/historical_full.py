import csv,json,datetime,os,re
from playwright.sync_api import sync_playwright
OUT="results";os.makedirs(OUT,exist_ok=True)
CUT=368*86400
def secs(v):
 try:return float(v)
 except:return 0
def T(v):
 try:
  if isinstance(v,(int,float)):return datetime.datetime.fromtimestamp(v/1000 if v>1e11 else v,tz=datetime.timezone.utc)
  return datetime.datetime.fromisoformat(str(v).replace("Z","+00:00")).astimezone(datetime.timezone.utc)
 except:return None
def walk(x,o):
 if isinstance(x,dict):
  t=next((T(x[k]) for k in x if k.lower() in ("time","timestamp","date","datetime","createtime","updatetime","x") and T(x[k])),None)
  if t:
   for k,v in x.items():
    if k.lower() in ("roi","roi_pct","value","y","return"):
     try:o.append((t,float(str(v).replace("%",""))));break
  for v in x.values():walk(v,o)
 elif isinstance(x,list):
  for v in x:walk(v,o)
with sync_playwright() as w:
 b=w.chromium.launch(headless=True);p=b.new_page();allrows=[];ep="https://www.binance.com/bapi/futures/v1/public/future/common/strategy/landing-page/queryTopStrategy"
 for pg in range(1,110):
  j=p.request.post(ep,data={"page":pg,"rows":100,"strategyType":1,"symbol":"","zone":"","sort":"pnl"}).json();d=j.get("data") or [];allrows+=d
  if len(d)<100:break
 cand=[x for x in allrows if secs(x.get("runningTime"))>=CUT]
 out=[]
 for i,x in enumerate(cand,1):
  sid=x.get("strategyId")
  try:
   z=p.request.post("https://www.binance.com/bapi/futures/v1/public/future/common/strategy/landing-page/queryRoiChart",data={"strategyId":sid,"streamerStrategyType":"SPOT_GRID"}).json().get("data") or [];a=[];walk(z,a)
   r={"strategyId":sid,"symbol":x.get("symbol"),"minInvestment":x.get("minInvestment"),"runningTime":x.get("runningTime"),"roi_now":x.get("roi"),"roi_2025_10_01":"","roi_2026_09_30":"","date_2025_10_01":"","date_2026_09_30":""}
   for d,k in zip((datetime.datetime(2025,10,1,tzinfo=datetime.timezone.utc),datetime.datetime(2026,9,30,tzinfo=datetime.timezone.utc)),(("roi_2025_10_01","date_2025_10_01"),("roi_2026_09_30","date_2026_09_30"))):
    if a:
     t,v=min(a,key=lambda q:abs((q[0]-d).total_seconds()))
     if abs((t-d).days)<=7:r[k[0]]=v;r[k[1]]=t.isoformat()
   out.append(r)
  except Exception:pass
  if i%100==0:print(i)
 with open(f"{OUT}/historical_candidates.csv","w",newline="",encoding="utf-8") as f:
  wri=csv.DictWriter(f,fieldnames=out[0] if out else ["strategyId"]);wri.writeheader();wri.writerows(out)
 with open(f"{OUT}/all_spot_active.csv","w",newline="",encoding="utf-8") as f:
  wri=csv.DictWriter(f,fieldnames=sorted({k for x in allrows for k in x}),extrasaction="ignore");wri.writeheader();wri.writerows(allrows)
 print("ALL",len(allrows),"CANDIDATES",len(cand),"HIST",len(out));b.close()