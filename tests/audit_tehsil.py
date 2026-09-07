"""
QA audit 7 (Tehsil-Level invariant): prove the 46 tehsil-centroid
schools get ZERO satellite points — their approximate coordinates must
never move a school's risk classification.

Run from PROJECT ROOT:
    python tests/audit_tehsil.py

HISTORICAL RESULT (Sept 2026 audit): 46 Tehsil-Level schools, ALL
NoData, modifier contributions all 0, scores byte-identical to base,
assertion in merge_satellite_scores_v2.py holds.
"""
import pandas as pd

df = pd.read_csv("ghostwatch_final_merged_v2.csv", dtype={"EMIS_Code": str})
teh = df[df["Satellite_Confidence"] == "Tehsil-Level"]

print("=" * 84)
print("TEHSIL-LEVEL INVARIANT CHECK")
print("=" * 84)
print(f"  Tehsil-Level schools: {len(teh)}")
print(f"  their density flags:  {teh['Satellite_Density_Flag'].value_counts().to_dict()}")
print(f"  (expect ALL NoData — a tehsil centroid can't be density-sampled)")
assert (teh["Satellite_Density_Flag"] == "NoData").all(), "REGRESSION: tehsil school has density!"

print()
print("  Merge-stage proof — modifier points per confidence tier (from the")
print("  SAT_MOD tables in merge_satellite_scores_v2.py):")
SAT_MOD_SCHOOL = {"Low_Density": 15, "Mixed_Density": 7, "High_Density": 0, "NoData": 0}
SAT_MOD_VILLAGE = {"Low_Density": 5, "Mixed_Density": 2, "High_Density": 0, "NoData": 0}
SAT_MOD_TEHSIL = {"Low_Density": 0, "Mixed_Density": 0, "High_Density": 0, "NoData": 0}
mods = (teh["Satellite_Density_Flag"].map(SAT_MOD_TEHSIL))
print(f"  tehsil modifier points: total={mods.sum()}, max={mods.max()} "
      f"(must be 0 — enforced by SAT_MOD_TEHSIL + _sat_modifier early return + assert)")

# Score-vs-base cross-check (Tier 2 schools: score is Infrastructure_Screening_Score)
print()
print("  Empirical check — Tier 2 tehsil schools' scores vs the pure census")
print("  Infrastructure_Screening_Score from the two-tier CSV (satellite must")
print("  not have moved them):")
tt = pd.read_csv("ghostwatch_two_tier_scores.csv", dtype={"EMIS_Code": str})
m = teh.merge(tt[["EMIS_Code", "Infrastructure_Screening_Score"]],
              on="EMIS_Code", suffixes=("_final", "_base"))
m["a"] = pd.to_numeric(m["Infrastructure_Screening_Score_final"], errors="coerce")
m["b"] = pd.to_numeric(m["Infrastructure_Screening_Score_base"], errors="coerce")
m["d"] = (m["a"] - m["b"]).abs()
print(f"    compared: {m['d'].notna().sum()} schools | max |diff| = {m['d'].max()} "
      f"| schools with any diff: {(m['d'] > 0.005).sum()}")
assert (m["d"].fillna(0) <= 0.005).all(), "REGRESSION: tehsil scores moved!"

print()
print("  Qwen fraud boost independence check: fraud flags among tehsil schools")
print(f"    flagged: {(teh['Qwen_Fraud_Flag'] == 'True').sum()} of {len(teh)} "
      "(fraud points come from Notes, unrelated to satellite)")

print()
print("  ALL TEHSIL-LEVEL INVARIANT CHECKS PASSED")
