import math, time
from datetime import datetime, timezone
import numpy as np, pandas as pd, requests

SYMBOL="TSTUSDT"; INTERVAL="15m"; FEE=0.001
GRIDS=[8,12,16,20,24,30]
RANGES={
"narrow":(0.01525,0.01775),
"baseline":(0.01500,0.01800),
"medium":(0.01450,0.01850),
"wide":(0.01400,0.01900),
}
START="2026-01-01"
OUT="results/tst_grid_sweep.csv"

def load():
    s=int(datetime.fromisoformat(START).replace(tzinfo=timezone.utc).timestamp()*1000)
    rows=[]
    while True:
        u="https://data-api.binance.vision/api/v3/klines"
        p={"symbol":SYMBOL,"interval":INTERVAL,"startTime":s,"limit":1000}
        x=requests.get(u,params=p,timeout=30); x.raise_for_status(); a=x.json()
        if not a: break
        rows += a; n=a[-1][0]
        if len(a)<1000: break
        s=n+1; time.sleep(.05)
    return pd.DataFrame([(r[0],float(r[4])) for r in rows],columns=["ts","close"]).drop_duplicates("ts")

def sim(px,lo,hi,n):
    lv=np.geomspace(lo,hi,n+1); cash=100.; base=0.; trades=0; peak=100.; mdd=0.
    # Start with half capital in quote/base, then emulate filled grid crossings.
    base=cash/(2*px[0]); cash/=2
    for p in px:
        if p<lo or p>hi: 
            eq=cash+base*p
        else:
            j=np.searchsorted(lv,p)-1; j=max(0,min(n-1,j))
            q=max(0.0,100/(2*lv[j]))
            if p<=lv[j] and cash>=q*lv[j]*(1+FEE):
                cash-=q*lv[j]*(1+FEE); base+=q; trades+=1
            elif p>=lv[j+1] and base>=q:
                base-=q; cash+=q*lv[j+1]*(1-FEE); trades+=1
            eq=cash+base*p
        peak=max(peak,eq); mdd=min(mdd,(eq/peak-1)*100)
    return (cash+base*px[-1]-100)/100*100,mdd,trades

df=load()
if len(df)<1000: raise RuntimeError("Insufficient TSTUSDT data")
p=df.close.to_numpy()
cut1=int(len(p)*.6); cut2=int(len(p)*.8)
rows=[]
for name,(lo,hi) in RANGES.items():
    for n in GRIDS:
        vals=[]
        for a,b,label in [(0,cut1,"TRAIN"),(cut1,cut2,"VALIDATION"),(cut2,len(p),"HOLDOUT")]:
            roi,mdd,tr=sim(p[a:b],lo,hi,n); vals.append((roi,mdd,tr))
        rows.append({"range":name,"grids":n,
                     "train_roi":vals[0][0],"train_mdd":vals[0][1],"train_trades":vals[0][2],
                     "validation_roi":vals[1][0],"validation_mdd":vals[1][1],"validation_trades":vals[1][2],
                     "holdout_roi":vals[2][0],"holdout_mdd":vals[2][1],"holdout_trades":vals[2][2],
                     "lower":lo,"upper":hi})
out=pd.DataFrame(rows)
out["validation_score"]=out.validation_roi/(out.validation_mdd.abs().clip(lower=0.1))
out=out.sort_values("validation_score",ascending=False)
out.to_csv(OUT,index=False)
best=out.iloc[0]
with open("results/tst_grid_sweep_summary.md","w") as f:
    f.write("# TSTUSDT Grid Sweep\n\n")
    f.write(f"Data: {datetime.fromtimestamp(df.ts.iloc[0]/1000,timezone.utc):%Y-%m-%d} → {datetime.fromtimestamp(df.ts.iloc[-1]/1000,timezone.utc):%Y-%m-%d}. {len(df):,} candles 15m.\n\n")
    f.write("Comparative simulator only; not Binance's internal backtest. Fee=0.10%.\n\n")
    f.write(f"## Best validation\n**{best['range']} / {int(best['grids'])} grids** — validation ROI {best.validation_roi:.2f}%, MDD {best.validation_mdd:.2f}%; holdout ROI {best.holdout_roi:.2f}%, MDD {best.holdout_mdd:.2f}%, trades {int(best.holdout_trades)}.\n\n")
    f.write(out.head(10).to_markdown(index=False))
