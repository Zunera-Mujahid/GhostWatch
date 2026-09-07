"""Verify the new satellite risk filter + high_first sort on the live API."""
import json
import urllib.request

def get(path):
    return json.loads(urllib.request.urlopen("http://127.0.0.1:8000" + path).read())

# 1. High First sort: page 1 must lead with High (Low_Density) badges
d = get("/schools?sort=high_first&limit=25")
badges = [
    "High" if s["Satellite_Density_Flag"] == "Low_Density"
    else "Medium" if s["Satellite_Density_Flag"] == "Mixed_Density"
    else "Low" if s["Satellite_Density_Flag"] == "High_Density"
    else "NotVerified"
    for s in d["items"]
]
print("1. sort=high_first, first 12 badges:", badges[:12])
print("   run structure:", [(b, badges.count(b)) for b in dict.fromkeys(badges)])

# 2-5. satellite filters
for level, expect in [("High", 6), ("Medium", 4), ("Low", 237), ("NotVerified", 53)]:
    d = get(f"/schools?satellite={level}&limit=300")
    flags = set(str(s["Satellite_Density_Flag"]) for s in d["items"])
    print(f"2. satellite={level}: total={d['total']} (expect {expect}), flags={flags}")

# 6. backward compat: flagged_first alone
d = get("/schools?sort=flagged_first&limit=6")
print("3. flagged_first alone: first 6 fraud flags:", [s["Qwen_Fraud_Flag"] for s in d["items"]])

# 7. combined
d = get("/schools?sort=flagged_first,high_first&limit=15")
flags = [s["Satellite_Density_Flag"] for s in d["items"]]
frauds = [s["Qwen_Fraud_Flag"] for s in d["items"]]
print("4. combined: first 15 density flags:", flags)
print("   combined: first 15 fraud flags: ", frauds)

# 8. lowercase robustness
d = get("/schools?satellite=high&limit=300")
print("5. satellite=high (lowercase): total =", d["total"])

# 9. bogus value -> graceful empty
d = get("/schools?satellite=Bogus&limit=25")
print("6. satellite=Bogus: HTTP 200, total =", d["total"])

# 10. default view unchanged
d = get("/schools?limit=25")
print("7. default (no params): first 3 scores:", [s["risk_score"] for s in d["items"][:3]])
