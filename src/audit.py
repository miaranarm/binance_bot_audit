import csv,re,os
from playwright.sync_api import sync_playwright
URL="https://www.binance.com/en/trading-bots"; OUT="results"; os.makedirs(OUT,exist_ok=True)
with sync_playwright() as p:
 b=p.chromium.launch(headless=True); page=b.new_page(viewport={"width":1440,"height":1200},locale="en-US")
 page.goto(URL,wait_until="domcontentloaded",timeout=60000); page.wait_for_timeout(12000)
 body=page.locator("body").inner_text(); open(f"{OUT}/rendered.txt","w",encoding="utf-8").write(body); b.close()
pat=re.compile(r"([A-Z0-9]{2,20}/(?:USDT|USDC|FDUSD|BTC|ETH)).{0,800}?PNL(?: \\(USD\\))?\\s*([+-]?[\\d,]+(?:\\.\\d+)?).{0,500}?ROI\\s*([+-]?[\\d,]+(?:\\.\\d+)?)%",re.I|re.S)
rows=[]
for m in pat.finditer(body): rows.append({"pair":m.group(1),"pnl_usd":float(m.group(2).replace(",","")),"roi_pct":float(m.group(3).replace(",","")),"source":URL})
rows=list({(x["pair"],x["pnl_usd"],x["roi_pct"]):x for x in rows}.values()); rows.sort(key=lambda x:(x["roi_pct"],x["pnl_usd"]),reverse=True)
with open(f"{OUT}/bots.csv","w",newline="",encoding="utf-8") as f:
 w=csv.DictWriter(f,fieldnames=["pair","pnl_usd","roi_pct","source"]); w.writeheader(); w.writerows(rows)
with open(f"{OUT}/top3.md","w",encoding="utf-8") as f:
 f.write("# Binance Public Bot Audit\\n\\n")
 for i,x in enumerate(rows[:3],1): f.write(f"## {i}. {x['pair']}\\n- ROI: {x['roi_pct']}%\\n- PNL: {x['pnl_usd']:,.2f} USD\\n")
print("Collected",len(rows),"public rows")