"""Verify the location-verification floor end-to-end via the live API."""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8000"

# wait for server
for _ in range(15):
    try:
        urllib.request.urlopen(BASE + "/stats", timeout=2)
        break
    except Exception:
        time.sleep(1)

for emis, name in [("37110078", "GGHS SALAR"), ("33250398", "GPS DOSA"),
                   ("31230405", "GGPS Riaz Khan (control — was already Medium)")]:
    d = json.loads(urllib.request.urlopen(f"{BASE}/schools/{emis}").read())
    verdict = {"High": "YES", "Medium": "POSSIBLE", "Low": "NO"}[d["audit_priority"]]
    print(f"{emis} {d['School_Name'][:40]:42s}")
    print(f"   Priority: {d['audit_priority']} -> dashboard verdict: {verdict}")
    print(f"   score: {d['risk_score']} | satellite reason: {d['Satellite_Risk_Reason'][:95]}...")
    print()

# stats reflect the new distribution
s = json.loads(urllib.request.urlopen(BASE + "/stats").read())
print("stats by_priority:", s["by_priority"], "(expect Medium=38, Low=239, High=23)")

# map check: SALAR marker should now be orange (Medium)
html = open("ghostwatch_map.html", encoding="utf-8").read()
import re
m = re.search(r"37110078.*?Priority:</b> <span[^>]*>(\w+)</span>", html, re.S)
print("map popup for SALAR shows Priority:", m.group(1) if m else "NOT FOUND")
m = re.search(r"33250398.*?Priority:</b> <span[^>]*>(\w+)</span>", html, re.S)
print("map popup for DOSA shows Priority:", m.group(1) if m else "NOT FOUND")
