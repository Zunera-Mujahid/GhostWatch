"""
QA audit 6 (census source viewer): the /census/* endpoints that back the
"View this school's row in the census file" feature.

Run from PROJECT ROOT (requires backend running on :8000):
    python tests/audit_census.py

Covers:
  * /census/meta          provenance present and internally consistent
                           with census_source/provenance.json + the CSV
  * /census/locate/{emis} EVERY Real (Census) school (251) must be found,
                           with its emiscode echoed back
  * /census/rows          windowing in file order; the located school's
                           row appears inside its window
  * /census/search        emiscode prefix, name substring, case
                           insensitivity, regex metacharacters treated
                           literally (never a 500)
  * error handling        unknown EMIS -> 404

HISTORICAL RESULT (Sept 2026): all pass — 251/251 located, 0 missing.
"""
import csv
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

BASE = "http://127.0.0.1:8000"

failures = []


def get(path):
    """Return (status, body) — never raises on HTTP errors."""
    try:
        r = urllib.request.urlopen(BASE + path)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


df = pd.read_csv("ghostwatch_final_merged_v2.csv", dtype={"EMIS_Code": str})
census_schools = df[df["Data_Type"] == "Real (Census)"]

print("=" * 90)
print("CENSUS AUDIT 1: /census/meta provenance")
print("=" * 90)
st, body = get("/census/meta")
meta = json.loads(body)
prov = json.loads(Path("census_source/provenance.json").read_text(encoding="utf-8"))
check("meta returns 200", st == 200)
check("title names the census", "Census" in str(meta.get("title", "")))
check("sha256 matches provenance.json", meta.get("sha256") == prov.get("sha256"))
check("dataset_url matches provenance.json",
      meta.get("dataset_url") == prov.get("dataset_url"))
# Count LOGICAL records: some census fields contain embedded newlines inside
# quoted CSV fields, so a raw line count would over-count by ~83.
with open("census_source/census_public_2018.csv", encoding="utf-8", newline="") as f:
    csv_rows = sum(1 for _ in csv.reader(f)) - 1
check("loaded_rows equals CSV logical record count", meta.get("loaded_rows") == csv_rows,
      f"meta={meta.get('loaded_rows')} csv={csv_rows}")
check("columns served (108 + provenance count)",
      len(meta.get("loaded_columns", [])) == prov.get("columns"))

print()
print("=" * 90)
print("CENSUS AUDIT 2: every Real (Census) school locatable (251 expected)")
print("=" * 90)
missing, name_matches = [], 0
for emis in census_schools["EMIS_Code"]:
    st, body = get(f"/census/locate/{urllib.parse.quote(emis)}")
    if st != 200:
        missing.append(emis)
        continue
    loc = json.loads(body)
    if not loc.get("found") or loc.get("emiscode") != emis:
        missing.append(emis)
    elif str(loc.get("school_name", "")).strip().lower() == \
            str(census_schools.loc[census_schools["EMIS_Code"] == emis,
                                   "School_Name"].iloc[0]).strip().lower():
        name_matches += 1
check(f"all {len(census_schools)} census schools located", not missing,
      f"missing: {missing[:5]}{'...' if len(missing) > 5 else ''}" if missing else "")
print(f"  INFO  census school_name matches dashboard School_Name: "
      f"{name_matches}/{len(census_schools)} (exact, case-insensitive)")

print()
print("=" * 90)
print("CENSUS AUDIT 3: /census/rows windowing (file order, target present)")
print("=" * 90)
sample = census_schools.iloc[0]
loc = json.loads(get(f"/census/locate/{sample['EMIS_Code']}")[1])
st, body = get(f"/census/rows?start={max(loc['index'] - 3, 0)}&count=8")
rows = json.loads(body)
idxs = [r["index"] for r in rows["rows"]]
check("rows returns 200", st == 200)
check("window is contiguous file order", idxs == list(range(idxs[0], idxs[0] + len(idxs))))
check("total equals loaded_rows", rows["total"] == meta["loaded_rows"])
check("target school inside its window",
      any(r["emiscode"] == sample["EMIS_Code"] for r in rows["rows"]))
st, body = get("/census/rows?start=99999999&count=5")
check("out-of-range start clamps, no crash", st == 200 and json.loads(body)["count"] >= 0)

print()
print("=" * 90)
print("CENSUS AUDIT 4: /census/search (prefix, substring, literalness)")
print("=" * 90)
st, body = get("/census/search?q=31321&limit=5")
s = json.loads(body)
check("emiscode prefix search works", st == 200 and s["total_matches"] > 0
      and all(str(r["emiscode"]).startswith("31321") for r in s["rows"]))
st, body = get("/census/search?q=mozang&limit=5")
s = json.loads(body)
check("case-insensitive name search", st == 200 and s["total_matches"] > 0
      and all("MOZANG" in str(r["school_name"]).upper() for r in s["rows"]))
st, body = get("/census/search?q=%28&limit=5")  # regex metachar '('
check("regex metachar treated literally (no 500)", st == 200)
st, body = get("/census/search?q=zzz_no_such_school_zzz&limit=5")
s = json.loads(body)
check("no-match search returns empty, not error", st == 200 and s["total_matches"] == 0)

print()
print("=" * 90)
print("CENSUS AUDIT 5: error handling")
print("=" * 90)
st, _ = get("/census/locate/99999999")
check("unknown EMIS -> 404", st == 404)

print()
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
    raise SystemExit(1)
print("RESULT: ALL CENSUS AUDIT CHECKS PASSED")
