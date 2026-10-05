import pandas as pd
from pathlib import Path

src = Path("results/current_multicriteria.csv")
out = Path("results/low_risk_high_return.csv")

df = pd.read_csv(src)
for c in ["minInvestment","roi","mdd7d","score","runningTime","matchedTrades"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")

# Two useful screens: strict MDD <=3%, and broader MDD <=5%.
base = df[(df["category"].eq("Spot Grid")) & (df["leverage"] <= 1) & (df["minInvestment"] <= 30)]
strict = base[base["mdd7d"] <= 0.03].copy()
broad = base[base["mdd7d"] <= 0.05].copy()

def rank(x):
    x = x.copy()
    # Favor ROI, then lower drawdown and smaller capital.
    x["risk_return"] = x["roi"] / (x["mdd7d"].clip(lower=0.001))
    return x.sort_values(["risk_return","roi","score"], ascending=[False,False,False])

strict = rank(strict)
broad = rank(broad)

cols = ["strategyId","category","strategyType","symbol","leverage","minInvestment","runningTime","roi","pnl","matchedTrades","mdd7d","score","risk_return"]
out.parent.mkdir(exist_ok=True)
with out.open("w", encoding="utf-8") as f:
    f.write("# STRICT MDD <= 3%\n")
    strict.head(100)[cols].to_csv(f, index=False)
    f.write("\n# BROAD MDD <= 5%\n")
    broad.head(100)[cols].to_csv(f, index=False)

print(f"BASE <=30 USDT: {len(base)}")
print(f"STRICT <=3% MDD: {len(strict)}")
print(f"BROAD <=5% MDD: {len(broad)}")
print("\nSTRICT TOP 20")
print(strict.head(20)[cols].to_string(index=False))
print("\nBROAD TOP 20")
print(broad.head(20)[cols].to_string(index=False))
