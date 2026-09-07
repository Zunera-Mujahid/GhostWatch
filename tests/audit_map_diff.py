"""
QA audit 2 (map integrity): EMIS-keyed comparison of old vs new map markers.

Run from PROJECT ROOT:
    python tests/audit_map_diff.py

Finds: added/removed markers, priority/score/name changes among common
markers, and cross-checks marker priority counts vs CSV truth.

HISTORICAL RESULT (Sept 2026 audit): old map was missing all 46
Tehsil-Level schools (247 vs 293 markers) — staleness, not corruption.
"""
import re
import pandas as pd

old = open("audit_map.bak.html", encoding="utf-8").read()
new = open("ghostwatch_map.html", encoding="utf-8").read()

PAT = (r"<b>([^<]+)</b><br><b>EMIS:</b> (\d+) &nbsp;\|&nbsp; "
       r"<b>Priority:</b> <span[^>]*>(\w+)</span> &nbsp;\|&nbsp; "
       r"<b>Score:</b> ([\d.]+)<br>")

def parse(m):
    pops = re.findall(PAT, m)
    return {emis: (name, pri, score) for name, emis, pri, score in pops}

o, n = parse(old), parse(new)
print("old map markers:", len(o), "| new map markers:", len(n))
added = set(n) - set(o)
removed = set(o) - set(n)
print("added in new map:", len(added))
print("removed from new map:", len(removed))

df = pd.read_csv("ghostwatch_final_merged_v2.csv", dtype={"EMIS_Code": str})
tehsil = set(df.loc[df["Satellite_Confidence"] == "Tehsil-Level", "EMIS_Code"])
print("of added, Tehsil-Level schools:", len(added & tehsil))
print("of removed, Tehsil-Level schools:", len(removed & tehsil))
if removed - tehsil:
    print("  removed NON-tehsil (unexpected):", removed - tehsil)

common = set(o) & set(n)
prio_chg = [(e, o[e][1], n[e][1]) for e in common if o[e][1] != n[e][1]]
score_chg = [(e, o[e][2], n[e][2]) for e in common if o[e][2] != n[e][2]]
name_chg = [e for e in common if o[e][0] != n[e][0]]
print("common markers with PRIORITY change:", len(prio_chg), prio_chg[:10])
print("common markers with SCORE change:", len(score_chg), score_chg[:10])
print("common markers with NAME change:", len(name_chg))

# Old map priority counts vs dashboard truth
from collections import Counter
print()
print("old map priority counts:", dict(Counter(v[1] for v in o.values())))
print("new map priority counts:", dict(Counter(v[1] for v in n.values())))
csv_counts = df["Priority"].value_counts().to_dict()
print("CSV priority counts (all 300):", csv_counts)
no_coord = df[df["Latitude"].isna()]
print("CSV schools without coords (should be 7 synthetic):", len(no_coord),
      "| their priorities:", dict(Counter(no_coord["Priority"])))
