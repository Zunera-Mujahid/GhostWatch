"""
GhostWatch - Merge Satellite Density Flags into Tier 1/Tier 2 Scores (v2)
==========================================================================
Produces ghostwatch_final_merged_v2.csv: the single source of truth.

All numeric values below live in ghostwatch_config.py (single source of
truth shared with the other pipeline scripts):

Confidence-aware satellite modifiers:
  School-Level (unique coordinates):
      Low_Density   -> +15
      Mixed_Density -> +7
      High_Density  -> +0
      NoData        -> +0
  Village-Level (shared/duplicate coordinates with other schools):
      Low_Density   -> +5   (reduced — describes area, not building)
      Mixed_Density -> +2   (reduced)
      High_Density  -> +0   (unchanged)
      NoData        -> +0
  Tehsil-Level (tehsil-centroid fallback geocoding):
      ALL flags     -> +0   (ZERO points in every case.  A tehsil centroid
                             does not represent the school's actual location;
                             imagery and density estimates there are visual
                             context only and must NEVER move a school's
                             risk score or Priority classification.)
  Priority thresholds as elsewhere (High >= 50, Medium >= 20, Low < 20) plus
  the same escalation rules, and one satellite-specific floor:
  School-Level + Low_Density -> at least Medium (location-verification
  floor).  A school whose EXACT coordinates show no built-up area has its
  claimed location unverified by imagery — whether the cause is a ghost
  school or a bad geocode, a field visit is warranted, so Priority is
  floored at Medium.  Scores are NOT altered by this rule.
"""

from pathlib import Path
import pandas as pd

from ghostwatch_config import (
    FRAUD_PRIORITY_ESCALATION_MERGE,
    ILLEGAL_PRIORITY_ESCALATION,
    PRIORITY_HIGH_MIN,
    PRIORITY_MEDIUM_MIN,
    SAT_LOC_FLOOR_PRIORITY,
    SAT_MOD_SCHOOL,
    SAT_MOD_TEHSIL,
    SAT_MOD_VILLAGE,
    SCORE_CAP,
    STATUS_OVERRIDE_STATES,
)

ROOT = Path(__file__).resolve().parent

# -- Step 1: Load tier scores ------------------------------------------------
scores = pd.read_csv(ROOT / "ghostwatch_two_tier_scores.csv", dtype={"EMIS_Code": str})
# Normalize district name inconsistencies
for _col in ("District", "Tehsil"):
    if _col in scores.columns:
        scores[_col] = scores[_col].astype(str).str.replace("D.G.KHAN", "D.G. KHAN", regex=False)
print(f"Loaded tier scores: {len(scores)} rows")

# -- Step 2: Left-join satellite data (v2 grid-sampled) ----------------------
satellite = pd.read_csv(ROOT / "satellite_results_v2.csv", dtype={"EMIS_Code": str})
sat_cols = satellite[["EMIS_Code", "Latitude", "Longitude",
                       "Percent_Built_Earliest", "Percent_Built_Recent",
                       "Satellite_BuiltUp_Percent", "Satellite_Density_Flag"]].copy()

merged = scores.merge(sat_cols, on="EMIS_Code", how="left")
print(f"After left-join: {len(merged)} rows, "
      f"density flag matched: {merged['Satellite_Density_Flag'].notna().sum()}")

# -- Step 2a: Preserve tehsil-centroid geocoding from a previous run --------
# satellite_results_v2.csv has NO coordinates for the 46 tehsil-fallback
# schools, so a naive re-join would wipe their Latitude/Longitude (breaking
# the HD reference images and the map).  Restore coords + remember which
# schools are Tehsil-Level so Step 2b keeps that label on re-runs.
_prev_final = ROOT / "ghostwatch_final_merged_v2.csv"
_tehsil_emis = set()
if _prev_final.exists():
    _prev = pd.read_csv(_prev_final, dtype={"EMIS_Code": str})
    _tehsil_prev = _prev[_prev["Satellite_Confidence"] == "Tehsil-Level"]
    _tehsil_emis = set(_tehsil_prev["EMIS_Code"])
    _lat_map = _tehsil_prev.set_index("EMIS_Code")["Latitude"]
    _lon_map = _tehsil_prev.set_index("EMIS_Code")["Longitude"]
    _need = merged["EMIS_Code"].isin(_tehsil_emis) & merged["Latitude"].isna()
    merged.loc[_need, "Latitude"] = merged.loc[_need, "EMIS_Code"].map(_lat_map).values
    merged.loc[_need, "Longitude"] = merged.loc[_need, "EMIS_Code"].map(_lon_map).values
    print(f"Preserved tehsil-centroid coords for {_need.sum()} schools "
          f"({len(_tehsil_emis)} Tehsil-Level schools known from previous run)")

# -- Step 2b: Detect duplicate coordinates -> Village-Level -------------------
real_mask = merged["Data_Type"].astype(str).str.strip() != "Synthetic"
has_coords = merged["Latitude"].notna() & merged["Longitude"].notna()
coord_groups = merged[real_mask & has_coords].groupby(
    ["Latitude", "Longitude"]).size()
dup_pairs = set(coord_groups[coord_groups > 1].index)

merged["Satellite_Confidence"] = "School-Level"
for i in merged.index:
    if real_mask.iloc[i] and has_coords.iloc[i]:
        key = (merged.loc[i, "Latitude"], merged.loc[i, "Longitude"])
        if key in dup_pairs:
            merged.loc[i, "Satellite_Confidence"] = "Village-Level"
        # Tehsil-centroid coords stay Tehsil-Level on re-runs -- never
        # re-classify them as School/Village-Level just because multiple
        # schools share the same tehsil centroid.
        if merged.loc[i, "EMIS_Code"] in _tehsil_emis:
            merged.loc[i, "Satellite_Confidence"] = "Tehsil-Level"
# Synthetic / no-coords rows -> null confidence
merged.loc[~(real_mask & has_coords), "Satellite_Confidence"] = None

n_village = (merged["Satellite_Confidence"] == "Village-Level").sum()
n_school = (merged["Satellite_Confidence"] == "School-Level").sum()
print(f"Satellite_Confidence: {n_school} School-Level, {n_village} Village-Level")

# -- Step 3: Apply satellite modifier (confidence-aware) ---------------------

def _active_base_score(row):
    tier1 = row.get("Ghost_Risk_Score_Tier1")
    if pd.notna(tier1) and str(tier1).strip() != "":
        return float(tier1)
    tier2 = row.get("Infrastructure_Screening_Score")
    if pd.notna(tier2) and str(tier2).strip() != "":
        return float(tier2)
    return 0.0

merged["_base_score"] = merged.apply(_active_base_score, axis=1)

# Confidence-aware satellite modifier tables live in ghostwatch_config.py
# (SAT_MOD_SCHOOL / SAT_MOD_VILLAGE / SAT_MOD_TEHSIL).  Tehsil-centroid
# coords do NOT represent the school's actual location: zero points in
# every case — imagery and density estimates there are visual context only
# and must never move a school's risk score or Priority classification.

def _sat_modifier(row):
    flag = row.get("Satellite_Density_Flag")
    if pd.isna(flag):
        return 0
    confidence = str(row.get("Satellite_Confidence", "School-Level")).strip()
    if confidence == "Tehsil-Level":
        return 0  # tehsil centroid is not the school's location: zero points
    table = SAT_MOD_VILLAGE if confidence == "Village-Level" else SAT_MOD_SCHOOL
    return table.get(str(flag).strip(), 0)

merged["_sat_mod"] = merged.apply(_sat_modifier, axis=1)
merged["_adjusted_score"] = (merged["_base_score"] + merged["_sat_mod"]).clip(upper=SCORE_CAP)

# -- Step 4: Recalculate Priority --------------------------------------------

def _priority_from_score(score):
    if score >= PRIORITY_HIGH_MIN:
        return "High"
    if score >= PRIORITY_MEDIUM_MIN:
        return "Medium"
    return "Low"

merged["Priority"] = merged["_adjusted_score"].apply(_priority_from_score)

# Escalation rule 1: illegal-occupation bumps priority up one level
illegal_mask = merged["Building_Illegal_Occupation"].astype(str).str.strip().str.lower() == "yes"
merged.loc[illegal_mask, "Priority"] = merged.loc[illegal_mask, "Priority"].map(ILLEGAL_PRIORITY_ESCALATION)

# Escalation rule 2: Qwen_Fraud_Flag=True escalates Medium -> High
fraud_mask = merged["Qwen_Fraud_Flag"].astype(str).str.strip() == "True"
merged.loc[fraud_mask, "Priority"] = merged.loc[fraud_mask, "Priority"].map(FRAUD_PRIORITY_ESCALATION_MERGE)

# Escalation rule 3: School_Status floor — Non-Functional/Closed -> at least High
_status_floor = merged["School_Status"].astype(str).str.strip().isin(
    STATUS_OVERRIDE_STATES)
merged.loc[_status_floor, "Priority"] = "High"

# Escalation rule 4: satellite location-verification floor — a School-Level
# (exact-coordinate) school in a Low_Density area: imagery found almost no
# built-up at the school's claimed location, so the location is unverified.
# Whether that means ghost school or bad geocode, an audit visit is warranted:
# floor the Priority at Medium.  (Scores are untouched; synthetic rows are
# excluded because their Satellite_Confidence is nulled upstream.)
_sat_loc_floor = (
    (merged["Satellite_Confidence"].astype(str).str.strip() == "School-Level")
    & (merged["Satellite_Density_Flag"].astype(str).str.strip() == "Low_Density")
)
merged.loc[_sat_loc_floor, "Priority"] = merged.loc[_sat_loc_floor, "Priority"].map(SAT_LOC_FLOOR_PRIORITY)
if _sat_loc_floor.any():
    print(f"Location-verification floor: {int(_sat_loc_floor.sum())} School-Level "
          f"Low_Density school(s) floored to at least Medium priority")

# Write adjusted score back into the appropriate column
mask_tier1 = merged["Ghost_Risk_Score_Tier1"].notna() & (merged["Ghost_Risk_Score_Tier1"].astype(str).str.strip() != "")
mask_tier2 = ~mask_tier1
merged.loc[mask_tier1, "Ghost_Risk_Score_Tier1"] = merged.loc[mask_tier1, "_adjusted_score"].round(2)
merged.loc[mask_tier2, "Infrastructure_Screening_Score"] = merged.loc[mask_tier2, "_adjusted_score"].round(2)

# -- Step 5: Synthetic safety filter ----------------------------------------
# Force all geocoding + satellite fields to null for synthetic test schools,
# regardless of what upstream files contain.  Prevents the geocoding-leak bug
# where Nominatim resolved fake school names to city-center coordinates.
syn_mask = merged["Data_Type"].astype(str).str.strip() == "Synthetic"
SATELLITE_NULL_COLS = [
    "Percent_Built_Earliest", "Percent_Built_Recent",
    "Satellite_BuiltUp_Percent", "Satellite_Density_Flag",
]
GEOCODE_NULL_COLS = ["Latitude", "Longitude"]

for col in SATELLITE_NULL_COLS:
    if col in merged.columns:
        merged.loc[syn_mask, col] = None
if "Satellite_Confidence" in merged.columns:
    merged.loc[syn_mask, "Satellite_Confidence"] = None

for col in GEOCODE_NULL_COLS:
    if col not in merged.columns:
        merged[col] = None            # add column if missing
    merged.loc[syn_mask, col] = None

n_syn = syn_mask.sum()
print(f"\nSynthetic safety filter: nulled satellite + geocode fields for {n_syn} rows")

# -- Step 6: Clean up and save ------------------------------------------------
merged.drop(columns=["_base_score", "_sat_mod", "_adjusted_score"], inplace=True)

output_path = ROOT / "ghostwatch_final_merged_v2.csv"
merged.to_csv(output_path, index=False)
print(f"\nSaved: {output_path}  ({len(merged)} rows)")

# -- Step 7: Synthetic verification table ------------------------------------
print("\n" + "=" * 70)
print("SYNTHETIC SCHOOL VERIFICATION (all satellite/geocode fields must be null)")
print("=" * 70)
syn_rows = merged[merged["Data_Type"].astype(str).str.strip() == "Synthetic"]
check_cols = ["EMIS_Code", "School_Name", "Latitude", "Longitude",
              "Satellite_BuiltUp_Percent", "Satellite_Density_Flag",
              "Ghost_Risk_Score_Tier1", "Priority"]
check_cols = [c for c in check_cols if c in syn_rows.columns]
print(syn_rows[check_cols].to_string(index=False))

# Assert every satellite/geocode field is null
for col in SATELLITE_NULL_COLS + GEOCODE_NULL_COLS:
    if col in syn_rows.columns:
        leaked = syn_rows[col].notna().sum()
        assert leaked == 0, f"LEAK: {leaked} synthetic rows still have {col}!"
print("\nPASS: all satellite + geocode fields are null for every synthetic row")

# -- Step 8: Verification ----------------------------------------------------
print("\n" + "=" * 70)
print("VERIFICATION: EMIS 35220032 - GHSS MOZANG LAHORE")
print("=" * 70)
row = merged[merged["EMIS_Code"] == "35220032"]
if row.empty:
    print("  NOT FOUND!")
else:
    r = row.iloc[0]
    print(f"  School Name            : {r['School_Name']}")
    print(f"  Tier                   : {r['Tier']}")
    print(f"  Ghost_Risk_Score_Tier1 : {r['Ghost_Risk_Score_Tier1']}")
    print(f"  Satellite_Density_Flag : {r.get('Satellite_Density_Flag', 'N/A')}")
    print(f"  Satellite_BuiltUp_%    : {r.get('Satellite_BuiltUp_Percent', 'N/A')}")
    print(f"  Priority               : {r['Priority']}")
    print(f"  Qwen_Fraud_Flag        : {r['Qwen_Fraud_Flag']}")
    assert str(r["Priority"]).strip() == "High", f"Expected High, got {r['Priority']}"
    assert str(r["Qwen_Fraud_Flag"]).strip() == "True", f"Expected True, got {r['Qwen_Fraud_Flag']}"
    print("  PASS: Priority=High, Qwen_Fraud_Flag=True")

print(f"\nPriority distribution:")
print(merged["Priority"].value_counts().to_string())
print(f"\nSatellite_Density_Flag distribution:")
print(merged["Satellite_Density_Flag"].value_counts(dropna=False).to_string())
print(f"\nSatellite_Confidence distribution:")
print(merged["Satellite_Confidence"].value_counts(dropna=False).to_string())

# -- Tehsil-Level invariant: satellite must contribute ZERO points ----------
tehsil_rows = merged[merged["Satellite_Confidence"] == "Tehsil-Level"]
if len(tehsil_rows):
    _tehsil_mods = tehsil_rows.apply(_sat_modifier, axis=1)
    assert (_tehsil_mods == 0).all(), (
        "Tehsil-Level schools must never receive satellite points!")
    print(f"\nTehsil-Level invariant: {len(tehsil_rows)} schools x "
          f"0 satellite points  [PASS]")

# Modifier breakdown
village_mods = merged[merged["Satellite_Confidence"] == "Village-Level"]
if len(village_mods):
    vc = village_mods["Satellite_Density_Flag"].value_counts()
    print(f"\nVillage-Level density breakdown:")
    for flag, cnt in vc.items():
        mod_val = SAT_MOD_VILLAGE.get(str(flag).strip(), 0) if not pd.isna(flag) else 0
        print(f"  {flag}: {cnt} schools (modifier: +{mod_val})")

# -- Step 9: Compare with previous merge -------------------------------------
print("\n" + "=" * 70)
print("PRIORITY CHANGE: v2 (grid density) vs v1 (single-pixel)")
print("=" * 70)

old_path = ROOT / "ghostwatch_final_merged.csv"
if old_path.exists():
    old = pd.read_csv(old_path, dtype={"EMIS_Code": str})
    old = old[["EMIS_Code", "Priority"]].rename(columns={"Priority": "Priority_v1"})
    new = merged[["EMIS_Code", "Priority"]].rename(columns={"Priority": "Priority_v2"})
    compare = old.merge(new, on="EMIS_Code", how="inner")
    compare["changed"] = compare["Priority_v1"] != compare["Priority_v2"]
    n_changed = compare["changed"].sum()
    n_total = len(compare)
    print(f"\nSchools compared: {n_total}")
    print(f"Priority CHANGED:   {n_changed}  ({n_changed/n_total*100:.1f}%)")
    print(f"Priority UNCHANGED: {n_total - n_changed}  ({(n_total-n_changed)/n_total*100:.1f}%)")
    if n_changed > 0:
        print(f"\nBreakdown of changes:")
        changes = compare[compare["changed"]]
        for (v1, v2), count in changes.groupby(["Priority_v1", "Priority_v2"]).size().items():
            print(f"  {v1} -> {v2}: {count} schools")
        print(f"\nChanged schools detail:")
        for _, r in changes.iterrows():
            s = merged[merged["EMIS_Code"] == r["EMIS_Code"]].iloc[0]
            print(f"  {r['EMIS_Code']} | {str(s['School_Name'])[:40]:40s} | "
                  f"{r['Priority_v1']:6s} -> {r['Priority_v2']:6s} | "
                  f"Density: {s.get('Satellite_Density_Flag', 'N/A')}")
    emis_check = compare[compare["EMIS_Code"] == "35220032"]
    if not emis_check.empty:
        r = emis_check.iloc[0]
        print(f"\nEMIS 35220032 (GHSS MOZANG LAHORE):")
        print(f"  v1 Priority: {r['Priority_v1']}")
        print(f"  v2 Priority: {r['Priority_v2']}")
        print(f"  Status: {'UNCHANGED' if r['Priority_v1'] == r['Priority_v2'] else 'CHANGED'}")
else:
    print("  ghostwatch_final_merged.csv not found, skipping comparison.")
