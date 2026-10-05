import time
from datetime import datetime, timezone
import numpy as np, pandas as pd, requests

SYMBOL="TSTUSDT"; INTERVAL="15m"; FEE=0.001
GRIDS=[16,20,24,30,40,50]
RANGES=[
    (0.01500,0.01800,"REF"),
    (0.01450,0.01850,"WIDE1"),
    (0.01400,0.01900,"WIDE2"),
    (0.01350,0.01950,"WIDE3"),
    (0.01300,0.02000,"WIDE4"),
    (0.01250,0.02050,"WIDE5"),
]
INVESTMENTS=[21.61783,30.0,37.5,42.5,45.0]
START="2026-01-01"
OUT="results/tst_grid_sweep.csv"

def load():
    start=int(datetime.fromisoformat(START).replace(tzinfo=timezone.utc).timestamp()*1000)
    rows=[]
    while True:
        r=requests.get("https://data-api.binance.vision/api/v3/klines",
                       params={"symbol":SYMBOL,"interval":INTERVAL,"startTime":start,"limit":1000},
                       timeout=30)
        r.raise_for_status()
        a=r.json()
        if not a: break
        rows.extend(a)
        if len(a)<1000: break
        start=a[-1][0]+1
        time.sleep(.05)
    return pd.DataFrame(
        [(x[0],float(x[1]),float(x[2]),float(x[3]),float(x[4])) for x in rows],
        columns=["ts","open","high","low","close"],
    ).drop_duplicates("ts").reset_index(drop=True)

def sim(df, lo, hi, n, investment):
    levels=np.geomspace(lo,hi,n+1)
    p0=float(df.iloc[0].close)
    capital=float(investment)
    cash=capital/2
    base=(capital/2)/p0
    q=(capital/(2*n))/p0
    buy=set(i for i in range(n) if levels[i] < p0)
    sell=set(i for i in range(1,n+1) if levels[i] > p0)
    trades=matched=0
    peak=capital
    mdd=0.0

    def fill_buy(i):
        nonlocal cash,base,trades
        cost=q*levels[i]*(1+FEE)
        if i in buy and cash+1e-12 >= cost:
            cash-=cost; base+=q; buy.remove(i)
            if i+1 <= n: sell.add(i+1)
            trades+=1
            return True
        return False

    def fill_sell(i):
        nonlocal cash,base,trades,matched
        if i in sell and base+1e-12 >= q:
            cash+=q*levels[i]*(1-FEE); base-=q; sell.remove(i)
            if i-1 >= 0: buy.add(i-1)
            trades+=1; matched+=1
            return True
        return False

    for row in df.itertuples(index=False):
        o,h,l,c=map(float,(row.open,row.high,row.low,row.close))
        path=[o,l,h,c] if c>=o else [o,h,l,c]
        for a,b in zip(path,path[1:]):
            if b>a:
                for i in sorted([j for j in sell if a < levels[j] <= b]):
                    fill_sell(i)
            elif b<a:
                for i in sorted([j for j in buy if b <= levels[j] < a],reverse=True):
                    fill_buy(i)
        eq=cash+base*c
        peak=max(peak,eq)
        mdd=min(mdd,(eq/peak-1)*100)

    final=cash+base*float(df.iloc[-1].close)
    roi=(final-capital)/capital*100
    return roi,mdd,trades,matched,final

df=load()
if len(df)<1000: raise RuntimeError("Insufficient TSTUSDT data")

cut1=int(len(df)*.6); cut2=int(len(df)*.8)
rows=[]
for lo,hi,label in RANGES:
    for n in GRIDS:
        for investment in INVESTMENTS:
            vals=[]
            for a,b,period in [(0,cut1,"TRAIN"),(cut1,cut2,"VALIDATION"),(cut2,len(df),"HOLDOUT")]:
                vals.append(sim(df.iloc[a:b].copy(),lo,hi,n,investment))
            rows.append({
            "investment":investment,"range":label,"grids":n,"lower":lo,"upper":hi,
            "grid_width_pct":(hi/lo-1)*100,
            "train_roi":vals[0][0],"train_mdd":vals[0][1],"train_trades":vals[0][2],"train_matched":vals[0][3],"train_final":vals[0][4],
            "validation_roi":vals[1][0],"validation_mdd":vals[1][1],"validation_trades":vals[1][2],"validation_matched":vals[1][3],"validation_final":vals[1][4],
            "holdout_roi":vals[2][0],"holdout_mdd":vals[2][1],"holdout_trades":vals[2][2],"holdout_matched":vals[2][3],"holdout_final":vals[2][4],
        })

out=pd.DataFrame(rows)
out["validation_score"]=out.validation_roi/out.validation_mdd.abs().clip(lower=0.1)
out["safe5"]=out.validation_mdd.abs()<=5
out["beats130_holdout"]=(out.holdout_roi>130)
out=out.sort_values(["safe5","validation_score","holdout_roi"],ascending=[False,False,False])
out.to_csv(OUT,index=False)

best=out.iloc[0]
with open("results/tst_grid_sweep_summary.md","w") as f:
    f.write("# TST Progressive Range / Investment / Grid Sweep\n\n")
    f.write(f"Data: {datetime.fromtimestamp(df.ts.iloc[0]/1000,timezone.utc):%Y-%m-%d} → {datetime.fromtimestamp(df.ts.iloc[-1]/1000,timezone.utc):%Y-%m-%d}. {len(df):,} candles 15m.\n\n")
    f.write("Reference: TSTUSDT, geometric 0.01500–0.01800, 16 grids, native minimum investment 21.61783 USDT. Comparative simulator only; not Binance's internal engine.\n\n")
    f.write("Sweep: 5 progressive ranges × 6 grid counts × 5 investment levels = 150 configurations. Investments: 21.61783 / 30 / 37.5 / 42.5 / 45 USDT. Safety filter: validation MDD <=5%.\n\n")
    f.write(f"## Best validation result\n**{best['range']} / {int(best['grids'])} grids / {best['investment']:.5f} USDT** ({best.lower:.5f}–{best.upper:.5f}) — validation ROI {best.validation_roi:.2f}%, MDD {best.validation_mdd:.2f}%; holdout ROI {best.holdout_roi:.2f}%, MDD {best.holdout_mdd:.2f}%, matched orders {int(best.holdout_matched)}.\n\n")
    safe=out[out.safe5]
    beat=out[out.beats130_holdout]
    f.write(f"Configurations with validation MDD <=5%: {len(safe)} / {len(out)}.\n")
    f.write(f"Configurations with holdout ROI >130%: {len(beat)} / {len(out)}.\n\n")
    f.write("## Full ranking\n")
    cols=["investment","range","grids","lower","upper","grid_width_pct","validation_roi","validation_mdd","holdout_roi","holdout_mdd","holdout_matched","safe5"]
    f.write(out[cols].to_markdown(index=False))
