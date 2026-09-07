"""
fix_duplicate_coords.py
========================
Identifies real schools that share EXACT duplicate Latitude/Longitude with
other schools (a clear sign the geocoder fell back to a village centroid
rather than finding the actual school building).

For these schools:
  1. Nulls satellite fields (Satellite_Density_Flag, Satellite_BuiltUp_Percent,
     Percent_Built_Earliest, Percent_Built_Recent)
  2. Removes the satellite modifier from scores (+15 for Low_Density,
     +7 for Mixed_Density, +0 for High_Density)
  3. Recalculates Priority from the corrected base score
  4. Sets Satellite_Risk_Reason to an explanation of why the reading was
     invalidated (not left blank — that would be confusing)

Pipeline position: run BEFORE add_explanation_columns.py and
build_final_reason_and_map.py, which will regenerate Data_Risk_Reason,
Satellite_Risk_Reason, and Final_GhostSchool_Reason from the corrected data.
"""
import sys
import os
import pandas as pd
import numpy as np

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(ROOT, "ghostwatch_final_merged_v2.csv")

df = pd.read_csv(CSV_PATH, dtype={"EMIS_Code": str})
print(f"Loaded {len(df)} rows")

# ── Step 1: Identify duplicate coordinates among real schools ───────────
real_mask = df["Data_Type"] != "Synthetic"
has_coords = df["Latitude"].notna() & df["Longitude"].notna()
check = df[real_mask & has_coords].copy()

coord_counts = check.groupby(["Latitude", "Longitude"]).size()
dup_coords = coord_counts[coord_counts > 1]
print(f"\nDuplicate coordinate pairs found: {len(dup_coords)}")
for (lat, lon), cnt in dup_coords.items():
    schools = df[(df["Latitude"] == lat) & (df["Longitude"] == lon)
                 & real_mask & has_coords]
    names = schools[["EMIS_Code", "School_Name", "Satellite_Density_Flag"]].values
    print(f"  ({lat}, {lon}) — {cnt} schools:")
    for emis, name, flag in names:
        print(f"    {emis} {str(name)[:45]:45s}  {flag}")

dup_keys = set(zip(dup_coords.index.get_level_values(0),
                   dup_coords.index.get_level_values(1)))
dup_mask = pd.Series(
    [(real_mask.iloc[i] and has_coords.iloc[i]
      and (df.iloc[i]["Latitude"], df.iloc[i]["Longitude"]) in dup_keys)
     for i in range(len(df))],
    index=df.index
)

n_affected = dup_mask.sum()
print(f"\nSchools affected (duplicate coords): {n_affected}")

# ── Step 2: Record pre-change state ─────────────────────────────────────
SAT_MODIFIER = {"Low_Density": 15, "Mixed_Density": 7, "High_Density": 0, "NoData": 0}

def get_score(row):
    t1 = row.get("Ghost_Risk_Score_Tier1")
    if pd.notna(t1) and str(t1).strip():
        return float(t1), "Ghost_Risk_Score_Tier1"
    t2 = row.get("Infrastructure_Screening_Score")
    if pd.notna(t2) and str(t2).strip():
        return float(t2), "Infrastructure_Screening_Score"
    return 0.0, None

before = {}
for idx in df[dup_mask].index:
    r = df.loc[idx]
    score, col = get_score(r)
    flag = r.get("Satellite_Density_Flag", "")
    mod = SAT_MODIFIER.get(str(flag).strip(), 0) if not pd.isna(flag) else 0
    before[idx] = {
        "EMIS": r["EMIS_Code"],
        "Name": r["School_Name"],
        "Score_before": score,
        "Score_col": col,
        "Flag_before": flag if not pd.isna(flag) else "NaN",
        "Modifier": mod,
        "Priority_before": r["Priority"],
    }

# ── Step 3: Null satellite fields ───────────────────────────────────────
SAT_REASON_MSG = ("Satellite verification unreliable — coordinates matched a "
                  "shared village-center point, not this specific school's location")

for col in ["Satellite_Density_Flag", "Satellite_BuiltUp_Percent",
            "Percent_Built_Earliest", "Percent_Built_Recent"]:
    if col in df.columns:
        df.loc[dup_mask, col] = np.nan

df.loc[dup_mask, "Satellite_Risk_Reason"] = SAT_REASON_MSG

# ── Step 4: Remove satellite modifier from scores ───────────────────────
for idx in df[dup_mask].index:
    info = before[idx]
    if info["Score_col"] and info["Modifier"] > 0:
        new_score = round(info["Score_before"] - info["Modifier"], 2)
        df.loc[idx, info["Score_col"]] = new_score

# ── Step 5: Recalculate Priority ────────────────────────────────────────
def score_to_priority(score):
    if pd.isna(score):
        return "Low"
    if score >= 50:
        return "High"
    if score >= 20:
        return "Medium"
    return "Low"

ESCALATION = {"Low": "Medium", "Medium": "High", "High": "High"}

# Recalculate for ALL rows (safe — non-affected rows won't change)
for idx in range(len(df)):
    score, _ = get_score(df.loc[idx])
    priority = score_to_priority(score)

    # Escalation 1: illegal occupation
    illegal = str(df.loc[idx].get("Building_Illegal_Occupation", "")).strip().lower() == "yes"
    if illegal:
        priority = ESCALATION.get(priority, priority)

    # Escalation 2: Qwen fraud (Medium -> High)
    fraud = str(df.loc[idx].get("Qwen_Fraud_Flag", "")).strip() == "True"
    if fraud and priority == "Medium":
        priority = "High"

    # Escalation 3: School_Status floor
    status = str(df.loc[idx].get("School_Status", "")).strip()
    if status in ("Non-Functional", "Closed"):
        priority = "High"

    df.loc[idx, "Priority"] = priority

# ── Step 6: Report changes ──────────────────────────────────────────────
print("\n" + "=" * 78)
print("AFFECTED SCHOOLS — DETAIL")
print("=" * 78)
changes = []
for idx in sorted(before.keys()):
    info = before[idx]
    new_score, _ = get_score(df.loc[idx])
    new_priority = df.loc[idx]["Priority"]
    changed = info["Priority_before"] != new_priority
    print(f"\n  EMIS: {info['EMIS']}")
    print(f"  Name: {info['Name']}")
    print(f"  Score:   {info['Score_before']} -> {new_score}  (removed +{info['Modifier']} satellite modifier)")
    print(f"  Density: {info['Flag_before']} -> NoData")
    print(f"  Priority: {info['Priority_before']} -> {new_priority}  {'*** CHANGED ***' if changed else '(unchanged)'}")
    if changed:
        changes.append(info["EMIS"])

print(f"\n{'=' * 78}")
print(f"SUMMARY: {n_affected} schools had satellite data invalidated")
print(f"  Priority changed: {len(changes)} schools")
if changes:
    print(f"  EMIS codes with priority change: {', '.join(changes)}")
print(f"{'=' * 78}")

# ── Step 7: Verify satellite flag distribution ──────────────────────────
print(f"\nSatellite_Density_Flag distribution (after fix):")
print(df["Satellite_Density_Flag"].value_counts(dropna=False).to_string())
print(f"\nPriority distribution (after fix):")
print(df["Priority"].value_counts().to_string())

# ── Step 8: Save ────────────────────────────────────────────────────────
df.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
print(f"\nSaved: {CSV_PATH} ({len(df)} rows)")
