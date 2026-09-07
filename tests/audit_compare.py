"""
QA audit 1 (data integrity): compare freshly re-run pipeline outputs vs
pre-run backups — determinism + staleness check.

Run from PROJECT ROOT (not from tests/):
    python tests/audit_compare.py

Requires backup files audit_two_tier.bak.csv / audit_final.bak.csv /
audit_map.bak.html to exist next to the root CSVs (created by the auditor
before re-running the pipeline).
"""
import pandas as pd
import numpy as np

print("=" * 78)
print("A. TWO-TIER CSV: fresh re-run vs backup (determinism check)")
print("=" * 78)
new_tt = pd.read_csv("ghostwatch_two_tier_scores.csv", dtype={"EMIS_Code": str})
old_tt = pd.read_csv("audit_two_tier.bak.csv", dtype={"EMIS_Code": str})
print(f"  shape: new {new_tt.shape} vs old {old_tt.shape}")
print(f"  columns identical: {list(new_tt.columns) == list(old_tt.columns)}")
key_cols = ["EMIS_Code"]
num_cols = ["Ghost_Risk_Score_Tier1", "Infrastructure_Screening_Score"]
m = old_tt.merge(new_tt, on="EMIS_Code", suffixes=("_old", "_new"))
for c in num_cols:
    if c + "_old" in m.columns:
        d = (pd.to_numeric(m[c + "_old"], errors="coerce") -
             pd.to_numeric(m[c + "_new"], errors="coerce")).abs()
        print(f"  {c}: max |diff| = {d.max()}")
for c in ["Priority", "Qwen_Fraud_Flag", "Tier"]:
    if c in old_tt.columns and c in new_tt.columns:
        diff = (m[c + "_old"].astype(str) != m[c + "_new"].astype(str)).sum()
        print(f"  {c}: {diff} rows differ")

print()
print("=" * 78)
print("B. FINAL CSV: fresh re-run vs backup (staleness check)")
print("=" * 78)
new_f = pd.read_csv("ghostwatch_final_merged_v2.csv", dtype={"EMIS_Code": str})
old_f = pd.read_csv("audit_final.bak.csv", dtype={"EMIS_Code": str})
print(f"  shape: new {new_f.shape} vs old {old_f.shape}")
print(f"  columns identical: {list(new_f.columns) == list(old_f.columns)}")
m = old_f.merge(new_f, on="EMIS_Code", suffixes=("_old", "_new"))

for c in ["Ghost_Risk_Score_Tier1", "Infrastructure_Screening_Score",
          "Latitude", "Longitude", "Satellite_BuiltUp_Percent"]:
    d = (pd.to_numeric(m[c + "_old"], errors="coerce") -
         pd.to_numeric(m[c + "_new"], errors="coerce")).abs()
    print(f"  {c}: max |diff| = {d.max()}  (rows changed >0.005: {(d > 0.005).sum()})")
for c in ["Priority", "Qwen_Fraud_Flag", "Satellite_Density_Flag",
          "Satellite_Confidence", "Tier", "Confidence_Level", "School_Status"]:
    if c + "_old" in m.columns:
        diff = (m[c + "_old"].astype(str) != m[c + "_new"].astype(str)).sum()
        print(f"  {c}: {diff} rows differ")

print()
print("  Reason columns (text drift expected only in recency phrases):")
for c in ["Data_Risk_Reason", "Satellite_Risk_Reason", "Final_GhostSchool_Reason",
          "Qwen_Fraud_Reason"]:
    a = m[c + "_old"].fillna("")
    b = m[c + "_new"].fillna("")
    n_diff = (a != b).sum()
    print(f"  {c}: {n_diff}/300 rows differ")
    if n_diff and c == "Data_Risk_Reason":
        idx = a != b
        for i in m[idx].head(6).index:
            print(f"    EMIS {m.loc[i, 'EMIS_Code']}:")
            print(f"      OLD: {a[i][:150]}")
            print(f"      NEW: {b[i][:150]}")

print()
print("=" * 78)
print("C. MAP: fresh vs backup")
print("=" * 78)
with open("ghostwatch_map.html", encoding="utf-8") as f:
    new_map = f.read()
with open("audit_map.bak.html", encoding="utf-8") as f:
    old_map = f.read()
print(f"  old size: {len(old_map)} chars | new size: {len(new_map)} chars")
print(f"  identical: {new_map == old_map}")
if new_map != old_map:
    # count marker differences via score/priority strings
    import re
    old_prios = re.findall(r"Priority:</b> <span[^>]*>(\w+)</span>", old_map)
    new_prios = re.findall(r"Priority:</b> <span[^>]*>(\w+)</span>", new_map)
    print(f"  priority popup strings: old {len(old_prios)} markers, new {len(new_prios)} markers")
    from collections import Counter
    print(f"  old counts: {dict(Counter(old_prios))}")
    print(f"  new counts: {dict(Counter(new_prios))}")
    old_scores = re.findall(r"Score:</b> ([\d.]+)<br>", old_map)
    new_scores = re.findall(r"Score:</b> ([\d.]+)<br>", new_map)
    score_diffs = sum(1 for a, b in zip(old_scores, new_scores) if a != b)
    print(f"  score strings differing: {score_diffs} of {len(old_scores)}")
    old_reasons = old_map.count("<i>")
    new_reasons = new_map.count("<i>")
    print(f"  reason blocks: old {old_reasons} vs new {new_reasons}")
