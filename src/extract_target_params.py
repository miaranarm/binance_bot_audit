import csv,json
ids={"9161957","9177337","8799020"}
src="results/all_no_leverage_raw.csv"; out="results/target_bot_parameters.json"
hits=[]
with open(src,encoding="utf-8") as f:
    for r in csv.DictReader(f):
        if r.get("strategyId") in ids:
            # keep all native fields; isolate strategyParams if present
            hits.append(r)
with open(out,"w",encoding="utf-8") as f: json.dump(hits,f,ensure_ascii=False,indent=2)
print("FOUND",len(hits))
for r in hits:
    print(r.get("strategyId"), r.get("symbol"), r.get("strategyParams"), r.get("minInvestment"), r.get("gridCount"), r.get("lowerPrice"), r.get("upperPrice"), r.get("trailingUp"))
