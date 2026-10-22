#!/usr/bin/env python3
"""Per-pair Spot Grid recommendations from archived price history."""
from pathlib import Path
from datetime import datetime, timezone
import math, re
import numpy as np
import pandas as pd

R=Path("results"); OUT=R/"current_price.csv"; HIST=R/"history"
FEE=0.001; BUDGET=100.0; LB=72*3600; FW=96*3600; STEP=24*3600
PAT=re.compile(r"^(\d{8}T\d{6})Z\.csv\.gz$")
BASE=["symbol","minInvestment","currentPrice","priceLow","priceHigh","binancePriceLow","binancePriceHigh","Niveau"]
EXTRA=["recommendedPriceLow","recommendedPriceHigh","recommendedGridCount","rangePosition72hPct","zScore72h","setupSignal"]

def stamp(p):
 m=PAT.match(p.name)
 if not m:return None
 try:return datetime.strptime(m.group(1),"%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).timestamp()
 except ValueError:return None

def history(now):
 rows=[]
 for p in HIST.rglob("*.csv.gz"):
  t=stamp(p)
  if t is None or t>now+3600:continue
  try:
   d=pd.read_csv(p,compression="gzip",usecols=lambda c:c in {"symbol","currentPrice"})
   if not {"symbol","currentPrice"}.issubset(d):continue
   d["currentPrice"]=pd.to_numeric(d.currentPrice,errors="coerce")
   d=d[d.currentPrice.gt(0)&np.isfinite(d.currentPrice)]
   for s,g in d.groupby("symbol"):rows.append((str(s).strip(),t,float(g.currentPrice.median())))
  except (OSError,ValueError,pd.errors.ParserError):pass
 if not rows:return {}
 d=pd.DataFrame(rows,columns=["symbol","t","p"]).drop_duplicates(["symbol","t"]).sort_values("t")
 return {s:(g.t.to_numpy(float),g.p.to_numpy(float)) for s,g in d.groupby("symbol")}

def replay(p,lo,hi,n):
 if not 0<lo<hi or len(p)<2:return None
 levels=np.geomspace(lo,hi,n+1); bought=[False]*n
 qty=[BUDGET/n/levels[i] for i in range(n)]; profit=0.; cycles=0
 for a,b in zip(p[:-1],p[1:]):
  if b<a:
   for k in reversed([k for k in range(n) if a>levels[k]>=b]):bought[k]=True
  elif b>a:
   for k in [k for k in range(1,n+1) if a<levels[k]<=b]:
    j=k-1
    if bought[j]:
     profit+=qty[j]*((levels[j+1]-levels[j])-FEE*(levels[j+1]+levels[j]));cycles+=1;bought[j]=False
 outside=100*sum(x<lo or x>hi for x in p)/len(p)
 return profit/BUDGET*100,outside,cycles

def setup(t,p,current):
 recent=p[t>=t[-1]-10*86400]
 if len(recent)<12:return [None,None,None,None,None,"INSUFFICIENT_HISTORY"]
 windows=[]; start=t[0]+LB
 while start+FW<=t[-1]:
  i=int(np.searchsorted(t,start)); x=t[i]
  past=p[(t>=x-LB)&(t<=x)]; future=p[(t>=x)&(t<=x+FW)]
  if len(past)>=12 and len(future)>=12:windows.append((past,future))
  start=x+STEP
 if not windows:return [None,None,None,None,None,"INSUFFICIENT_BACKTEST"]
 q=np.quantile(recent,[0,.1,.2,.3,.7,.8,.9,1])
 lows=sorted(set(float(x) for x in q[:4] if x>0)); highs=sorted(set(float(x) for x in q[4:] if x>0))
 best=None
 for lo in lows:
  for hi in highs:
   if lo>=hi:continue
   for n in (5,6,8,10):
    vals=[replay(f,lo,hi,n) for _,f in windows]; vals=[v for v in vals if v]
    if not vals:continue
    avg=float(np.mean([v[0] for v in vals])); outside=float(np.mean([v[1] for v in vals]))
    score=avg-.03*outside
    cand=(score,avg,-outside,-n,lo,hi,n)
    if best is None or cand>best:best=cand
 if best is None:return [None,None,None,None,None,"INSUFFICIENT_BACKTEST"]
 _,_,_,_,lo,hi,n=best
 p72=p[t>=t[-1]-LB]
 if len(p72)<12:return [lo,hi,n,None,None,"INSUFFICIENT_72H"]
 mn,mx=float(np.min(p72)),float(np.max(p72))
 pos=100*(current-mn)/(mx-mn) if mx>mn else None
 sd=float(np.std(p72)); z=(current-float(np.mean(p72)))/sd if sd>0 else None
 signal="WAIT_HIGH_RANGE" if (pos is not None and pos>80) or (z is not None and z>.3) else ("WAIT_OUTSIDE_RANGE" if current<lo or current>hi else "READY_TO_REVIEW")
 return [lo,hi,n,pos,z,signal]

def main():
 if not OUT.exists():raise SystemExit(f"Missing {OUT}")
 d=pd.read_csv(OUT,dtype={"symbol":str})
 if not {"symbol","currentPrice"}.issubset(d):raise SystemExit("Required columns missing")
 d=d[[c for c in BASE if c in d.columns]].copy()
 for c in EXTRA:d[c]=None
 h=history(datetime.now(timezone.utc).timestamp())
 for i,r in d.iterrows():
  s=str(r.symbol).strip(); price=pd.to_numeric(pd.Series([r.currentPrice]),errors="coerce").iloc[0]
  if s not in h or not math.isfinite(float(price)) or price<=0:
   d.at[i,"setupSignal"]="NO_DATA";continue
  vals=setup(*h[s],float(price))
  for c,v in zip(EXTRA,vals):d.at[i,c]=v
 tmp=OUT.with_suffix(".csv.tmp");d.to_csv(tmp,index=False);tmp.replace(OUT)
 print(f"Per-pair historical Spot Grid settings written: {len(d)} pairs")

if __name__=="__main__":main()
