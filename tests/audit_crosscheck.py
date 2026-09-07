"""
QA audit 3 (cross-surface consistency): compare 5 schools across
DASHBOARD (API) / MAP (HTML popups) / CSV, plus an aggregate sweep of
all 300 schools.

Run from PROJECT ROOT (requires backend running on :8000):
    python tests/audit_crosscheck.py

HISTORICAL RESULT (Sept 2026 audit): all OK after the risk_score
int-truncation fix — 0 mismatches across 300 schools.
"""
import html
import json
import re
import urllib.request

import pandas as pd

BASE = "http://127.0.0.1:8000"

# 5-school mix: Tier1-High-flagged, Tier2-High, Tier1-Medium, Tier1-Low, Tier2-Tehsil
SCHOOLS = [
    ("35220032", "Tier 1 High + fraud-flagged"),
    ("31321032", "Tier 2 High (Non-Functional)"),
    ("36420071", "Tier 1 Medium"),
    ("33150025", "Tier 1 Low (cleanest)"),
    ("32110565", "Tier 2 High + Tehsil-Level"),
]

# ---- CSV (source of truth) ----
csv = pd.read_csv("ghostwatch_final_merged_v2.csv", dtype={"EMIS_Code": str}).set_index("EMIS_Code")

# ---- MAP (parse popups by EMIS) ----
map_html = open("ghostwatch_map.html", encoding="utf-8").read()
PAT = (r"<b>([^<]+)</b><br><b>EMIS:</b> (\d+) &nbsp;\|&nbsp; "
       r"<b>Priority:</b> <span[^>]*>(\w+)</span> &nbsp;\|&nbsp; "
       r"<b>Score:</b> ([\d.]+)<br>")
pops = {emis: {"name": n, "pri": p, "score": s}
        for n, emis, p, s in re.findall(PAT, map_html)}
# map reason: text inside <i>...</i> right after the <hr>
for emis, reason in re.findall(r"EMIS:</b> (\d+).*?<i>(.*?)</i>", map_html, re.S):
    if emis in pops:
        pops[emis]["reason"] = reason

def num_eq(a, b):
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return False

print("=" * 96)
print("CROSS-CHECK: DASHBOARD(API) vs MAP vs CSV  --  5 schools")
print("=" * 96)

for emis, label in SCHOOLS:
    print(f"\n--- {emis} | {label} ---")
    api = json.loads(urllib.request.urlopen(f"{BASE}/schools/{emis}").read())
    row = csv.loc[emis]
    mp = pops.get(emis)

    csv_score = row["Ghost_Risk_Score_Tier1"]
    if pd.isna(csv_score):
        csv_score = row["Infrastructure_Screening_Score"]
    checks = []

    # Name
    checks.append(("Name", api["School_Name"], mp["name"] if mp else "-",
                   row["School_Name"],
                   api["School_Name"] == row["School_Name"] and (mp is None or mp["name"] == row["School_Name"])))
    # Priority
    checks.append(("Priority", api["audit_priority"], mp["pri"] if mp else "-",
                   row["Priority"],
                   api["audit_priority"] == row["Priority"] and (mp is None or mp["pri"] == row["Priority"])))
    # Score
    checks.append(("Score", api["risk_score"], mp["score"] if mp else "-",
                   round(float(csv_score), 2),
                   num_eq(api["risk_score"], csv_score) and (mp is None or num_eq(mp["score"], csv_score))))
    # Final reason (dashboard = API field; map = popup <i> text)
    # The map popup stores the reason HTML-escaped with <br> line breaks,
    # so undo both before comparing text content.
    csv_reason = str(row["Final_GhostSchool_Reason"])
    map_reason = mp.get("reason", "") if mp else ""
    map_reason_clean = re.sub(
        r"\s+", " ", html.unescape(map_reason.replace("<br>", " "))).strip()
    checks.append(("Reason", api["Final_GhostSchool_Reason"][:60] + "…",
                   map_reason_clean[:60] + "…", csv_reason[:60] + "…",
                   api["Final_GhostSchool_Reason"] == csv_reason
                   and (mp is None or map_reason_clean == re.sub(r"\s+", " ", csv_reason).strip())))
    # Verdict the dashboard badge would show (Priority-based after fix)
    verdict = {"High": "YES", "Medium": "POSSIBLE", "Low": "NO"}[row["Priority"]]
    checks.append(("Verdict", verdict, "-", "-", verdict == {"High": "YES", "Medium": "POSSIBLE", "Low": "NO"}[api["audit_priority"]]))

    for name, dash, mpv, csvv, ok in checks:
        status = "OK " if ok else "MISMATCH"
        print(f"  [{status}] {name:8s} | API: {str(dash)[:58]:58s}")
        print(f"           {'':8s} | MAP: {str(mpv)[:58]:58s}")
        print(f"           {'':8s} | CSV: {str(csvv)[:58]:58s}")

# All-300 aggregate consistency: API list vs CSV
print()
print("=" * 96)
print("AGGREGATE: all 300 schools - API list vs CSV (score/priority/fraud flag)")
print("=" * 96)
api_list = []
skip = 0
while True:
    batch = json.loads(urllib.request.urlopen(
        f"{BASE}/schools?skip={skip}&limit=100").read())
    api_list.extend(batch["items"])
    skip += 100
    if skip >= batch["total"]:
        break
api_df = pd.DataFrame(api_list).set_index("EMIS_Code")
joined = csv.join(api_df, rsuffix="_api")
score_bad = [e for e in joined.index
             if not num_eq(joined.loc[e, "risk_score"],
                           joined.loc[e, "Ghost_Risk_Score_Tier1"]
                           if pd.notna(joined.loc[e, "Ghost_Risk_Score_Tier1"])
                           else joined.loc[e, "Infrastructure_Screening_Score"])]
pri_bad = [e for e in joined.index
           if joined.loc[e, "Priority"] != joined.loc[e, "audit_priority"]]
fraud_bad = [e for e in joined.index
             if str(joined.loc[e, "Qwen_Fraud_Flag"]) != str(joined.loc[e, "Qwen_Fraud_Flag_api"])]
name_bad = [e for e in joined.index
            if str(joined.loc[e, "School_Name"]) != str(joined.loc[e, "School_Name_api"])]
print(f"  score mismatches:   {len(score_bad)} {score_bad[:5]}")
print(f"  priority mismatches:{len(pri_bad)} {pri_bad[:5]}")
print(f"  fraud flag mismatches: {len(fraud_bad)} {fraud_bad[:5]}")
print(f"  name mismatches:    {len(name_bad)} {name_bad[:5]}")
print(f"  API returned {len(api_list)} schools (expect 300)")
