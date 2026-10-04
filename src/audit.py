import csv,re,os,requests
from bs4 import BeautifulSoup
URL="https://www.binance.com/en/trading-bots"; OUT="results"; os.makedirs(OUT,exist_ok=True)
r=requests.get(URL,headers={"User-Agent":"Mozilla/5.0"},timeout=30); r.raise_for_status()
open(f"{OUT}/raw.html","w",encoding="utf-8").write(r.text)
text=" ".join(BeautifulSoup(r.text,"html.parser").stripped_strings)
pat=re.compile(r"([A-Z0-9]{2,20}/(?:USDT|USDC|FDUSD|BTC|ETH)).{0,500}?PNL \\(USD\\)\s*([+-]?[\d,]+\.\d+).{0,250}?ROI\s*([+-]?[\d,]+\.\d+)%")
rows=[]
for m in pat.finditer(text):
 rows.append({"pair":m.group(1),"pnl_usd":float(m.group(2).replace(",","")),"roi_pct":float(m.group(3).replace(",","")),"source":URL})
rows=list({(x["pair"],x["pnl_usd"],x["roi_pct"]):x for x in rows}.values()); rows.sort(key=lambda x:(x["roi_pct"],x["pnl_usd"]),reverse=True)
with open(f"{OUT}/bots.csv","w",newline="",encoding="utf-8") as f:
 w=csv.DictWriter(f,fieldnames=["pair","pnl_usd","roi_pct","source"]); w.writeheader(); w.writerows(rows)
with open(f"{OUT}/top3.md","w",encoding="utf-8") as f:
 f.write("# Binance Public Bot Audit\n\n")
 for i,x in enumerate(rows[:3],1):
  f.write("## "+str(i)+". "+x["pair"]+"\n- ROI: "+str(x["roi_pct"])+"%\n- PNL: "+format(x["pnl_usd"],",.2f")+" USD\n")
print("Collected",len(rows),"public rows")
