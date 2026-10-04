import csv,json,datetime,os
from playwright.sync_api import sync_playwright
OUT="results";os.makedirs(OUT,exist_ok=True)
D=[datetime.datetime(2025,10,1,tzinfo=datetime.timezone.utc),datetime.datetime(2026,9,30,tzinfo=datetime.timezone.utc)]
def T(v):
 try:
  if isinstance(v,(int,float)):return datetime.datetime.fromtimestamp(v/1000 if v>1e11 else v,tz=datetime.timezone.utc)
  return datetime.datetime.fromisoformat(str(v).replace("Z","+00:00")).astimezone(datetime.timezone.utc)
 except:return None
def pts(x,o):
 if isinstance(x,dict):
  t=next((T(x[k]) for k in x if k.lower() in ("time","timestamp","date","datetime","createtime","updatetime","x") and T(x[k])),None)
  if t:
   for k,v in x.items():
    if k.lower() in ("roi","roi_pct","value","y","return"):
     try:o.append((t,float(str(v).replace("%",""))));break
  for v in x.values():pts(v,o)
 elif isinstance(x,list):
  for v in x:pts(v,o)
with sync_playwright() as w:
 b=w.chromium.launch(headless=True);p=b.new_page();rows=[];ep="https://www.binance.com/bapi/futures/v1/public/future/common/strategy/landing-page/queryTopStrategy"
 for pg in range(1,30):
  j=p.request.post(ep,data={"page":pg,"rows":100,"strategyType":1,"symbol":"","zone":"","sort":"pnl"}).json();rows+=j.get("data") or []
  if len(j.get("data") or [])<100:break
 out=[]
 for i,x in enumerate(rows,1):
  sid=x.get("strategyId");z=p.request.post("https://www.binance.com/bapi/futures/v1/public/future/common/strategy/landing-page/queryRoiChart",data={"strategyId":sid,"streamerStrategyType":"SPOT_GRID"}).json().get("data") or [];a=[];pts(z,a)
  r={"strategyId":sid,"symbol":x.get("symbol"),"minInvestment":x.get("minInvestment"),"runningTime":x.get("runningTime"),"roi_now":x.get("roi"),"roi_2025_10_01":"","roi_2026_09_30":"","date_2025_10_01":"","date_2026_09_30":""}
  for d,k in zip(D,(("roi_2025_10_01","date_2025_10_01"),("roi_2026_09_30","date_2026_09_30"))):
   if a:
    t,v=min(a,key=lambda q:abs((q[0]-d).total_seconds()))
    if abs((t-d).days)<=7:r[k[0]]=v;r[k[1]]=t.isoformat()
  out.append(r)
  if i%100==0:print(i)
 with open(f"{OUT}/spot_history_targets.csv","w",newline="",encoding="utf-8") as f:
  wri=csv.DictWriter(f,fieldnames=out[0]);wri.writeheader();wri.writerows(out)
 b.close()
print("OK",len(out))