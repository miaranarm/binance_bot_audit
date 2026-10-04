import csv,re,os
from playwright.sync_api import sync_playwright
URL="https://www.binance.com/en/trading-bots"; OUT="results"; os.makedirs(OUT,exist_ok=True)
with sync_playwright() as p:
 b=p.chromium.launch(headless=True); pg=b.new_page(viewport={"width":1440,"height":1200},locale="en-US"); pg.goto(URL,wait_until="domcontentloaded",timeout=60000); pg.wait_for_timeout(12000); body=pg.locator("body").inner_text(); open(f"{OUT}/rendered.txt","w",encoding="utf-8").write(body); b.close()
pat=re.compile(r"([A-Z0-9]{2,20}/(?:USDT|USDC|FDUSD|BTC|ETH))\\s+Perp\\s+(.+?)\\s+(?:Create)\\s+PNL \\(USD\\)\\s+([+-]?[\\d,]+(?:\\.\\d+)?)\\s+ROI\\s+([+-]?[\\d,]+(?:\\.\\d+)?)%\\s+Runtime\\s+(.+?)\\s+Min\\. Investment\\s+([\\d,]+(?:\\.\\d+)?)\\s+\\w+\\s+24H/Total Matched Trades\\s+([\\d,]+)/([\\d,]+)\\s+7D MDD\\s+([\\d.]+)%",re.S)
rows=[]
for m in pat.finditer(body):
 pair,meta,pnl,roi,run,inv,t24,tall,mdd=m.groups(); rows.append({"pair":pair,"meta":meta.strip(),"pnl_usd":float(pnl.replace(",","")),"roi_pct":float(roi),"runtime":run.strip(),"min_investment":float(inv.replace(",","")),"trades_24h":int(t24.replace(",","")),"trades_total":int(tall.replace(",","")),"mdd7d_pct":float(mdd),"source":URL})
rows=list({(x["pair"],x["pnl_usd"],x["roi_pct"],x["runtime"],x["trades_total"]):x for x in rows}.values())
for x in rows: x["score"]=x["roi_pct"]/(1+x["mdd7d_pct"])*min(1,x["trades_total"]/500)
rows.sort(key=lambda x:x["score"],reverse=True)
fields=["pair","meta","pnl_usd","roi_pct","runtime","min_investment","trades_24h","trades_total","mdd7d_pct","score","source"]
with open(f"{OUT}/bots.csv","w",newline="",encoding="utf-8") as f: w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
with open(f"{OUT}/top3.md","w",encoding="utf-8") as f:
 f.write("# Binance Public Bot Audit\\n\\n")
 for i,x in enumerate(rows[:3],1): f.write(f"## {i}. {x['pair']} — {x['meta']}\\n- ROI: {x['roi_pct']:.2f}% | PNL: {x['pnl_usd']:,.2f} USD\\n- Runtime: {x['runtime']} | 7D MDD: {x['mdd7d_pct']:.2f}% | Trades: {x['trades_total']:,}\\n- Score: {x['score']:.3f}\\n")
print("Collected",len(rows),"public marketplace rows")