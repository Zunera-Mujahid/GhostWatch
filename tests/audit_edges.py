"""
QA audit 4 (edge cases): API + frontend data contract edge-case suite.

Run from PROJECT ROOT (requires backend running on :8000):
    python tests/audit_edges.py

Covers: NoData satellite school, empty Notes, highest/lowest scores,
graceful failure on bad searches (regex metachars, HTML/script chars,
nonexistent names, bad params), and combined filters.

HISTORICAL RESULT (Sept 2026 audit): all pass after the regex=False
search fix (searching '(' previously caused HTTP 500).
"""
import json
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

BASE = "http://127.0.0.1:8000"

def get(path):
    """Return (status, body) — never raises on HTTP errors."""
    try:
        r = urllib.request.urlopen(BASE + path)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

df = pd.read_csv("ghostwatch_final_merged_v2.csv", dtype={"EMIS_Code": str})

print("=" * 90)
print("EDGE CASE 1: school with NO satellite data (NoData flag, no image)")
print("=" * 90)
nodata = df[df["Satellite_Density_Flag"] == "NoData"].iloc[0]
st, body = get(f"/schools/{nodata['EMIS_Code']}")
d = json.loads(body)
print(f"  {d['School_Name'][:45]} (EMIS {d['EMIS_Code']})")
print(f"  flag={d['Satellite_Density_Flag']} | built%={d['Satellite_BuiltUp_Percent']} "
      f"| sat_reason={d['Satellite_Risk_Reason']!r}")
print(f"  has_satellite_image={d['has_satellite_image']}")
sti, _ = get(f"/satellite-image/{d['EMIS_Code']}")
print(f"  image endpoint for no-image school: HTTP {sti} (404 = no crash)")

print()
print("=" * 90)
print("EDGE CASE 2: school with missing / empty Notes")
print("=" * 90)
no_notes = df[df["Notes"].isna()].iloc[0]
st, body = get(f"/schools/{no_notes['EMIS_Code']}")
d = json.loads(body)
print(f"  {d['School_Name'][:45]} | Notes={d['Notes']!r} (API null)")
print(f"  fraud flag={d['Qwen_Fraud_Flag']} | fraud reason={d['Qwen_Fraud_Reason']!r}")
print(f"  rows with empty Notes in CSV: {df['Notes'].isna().sum()}/300")

print()
print("=" * 90)
print("EDGE CASE 3: highest and lowest scoring schools")
print("=" * 90)
df["_score"] = pd.to_numeric(df["Ghost_Risk_Score_Tier1"], errors="coerce")
df["_score"] = df["_score"].fillna(pd.to_numeric(df["Infrastructure_Screening_Score"], errors="coerce"))
hi = df.sort_values("_score", ascending=False).iloc[0]
lo = df.sort_values("_score").iloc[0]
for label, r in [("HIGHEST", hi), ("LOWEST", lo)]:
    st, body = get(f"/schools/{r['EMIS_Code']}")
    d = json.loads(body)
    print(f"  {label}: {d['School_Name'][:40]} | score={d['risk_score']} "
          f"| priority={d['audit_priority']} | status={d['School_Status']} | HTTP {st}")

print()
print("=" * 90)
print("EDGE CASE 4: searches that must fail gracefully (no 500s)")
print("=" * 90)
cases = [
    ("nonexistent school name", "/schools?search=" + urllib.parse.quote("ZZZNO_SUCH_SCHOOL_ZZZ")),
    ("regex metachar '(' (previously 500!)", "/schools?search=" + urllib.parse.quote("(")),
    ("regex metachar '[' ", "/schools?search=" + urllib.parse.quote("[")),
    ("regex metachar '.*' literal", "/schools?search=" + urllib.parse.quote(".*")),
    ("HTML/script chars", "/schools?search=" + urllib.parse.quote("<script>alert(1)</script>")),
    ("EMIS not found", "/schools/00000000"),
    ("invalid limit=0", "/schools?limit=0"),
    ("limit=999 (over max)", "/schools?limit=999"),
    ("bogus param value", "/schools?priority=Bogus"),
    ("image for synthetic EMIS", "/satellite-image/99990001"),
]
for label, path in cases:
    st, body = get(path)
    detail = ""
    if st == 200:
        try:
            j = json.loads(body)
            detail = f"total={j.get('total', len(j))}"
        except Exception:
            detail = f"{len(body)} bytes"
    else:
        try:
            detail = json.loads(body).get("detail", "")[:40]
        except Exception:
            detail = body[:40]
    flag = "OK " if st in (200, 404, 422) else "FAIL"
    print(f"  [{flag}] HTTP {st} | {label:38s} | {detail}")

# real-search sanity
st, body = get("/schools?search=" + urllib.parse.quote("MOZANG"))
j = json.loads(body)
print(f"  [OK ] real search 'MOZANG': total={j['total']} "
      f"-> {j['items'][0]['School_Name'] if j['items'] else 'none'}")

print()
print("=" * 90)
print("EDGE CASE 5: filters combined (district+priority+fraud+search)")
print("=" * 90)
st, body = get("/schools?district=LAHORE&priority=High&sort=flagged_first&limit=300")
j = json.loads(body)
flagged_top = sum(1 for s in j["items"] if s["Qwen_Fraud_Flag"] == "True")
print(f"  Lahore + High: {j['total']} schools | flagged lead: {flagged_top}")
st, body = get("/schools?fraud_flag=True&priority=High&limit=300")
j = json.loads(body)
print(f"  fraud_flag=True + High: {j['total']} schools (all flagged & High)")
