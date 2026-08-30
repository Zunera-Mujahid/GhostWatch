"""
ghostwatch_scoring.py
=====================

Two-tier ghost-school risk scoring for the GhostWatch dataset (300 schools).

NOTE ON METHODOLOGY:
  No single official published "ghost school formula" exists -- this was
  verified via research across PMIU, PESRP, and Punjab School Education
  Department publications.  The weighting scheme below is an ORIGINAL,
  transparently-justified model informed by the World Bank's Service Delivery
  Indicators (SDI) framework, which treats teacher ABSENCE (not vacancy) as
  the primary service-delivery indicator for education globally.

  The SDI approach distinguishes between:
    - Paper metrics (teacher vacancy / fill rate) -- easily gamed
    - Physical-presence metrics (attendance on visit day) -- hard to fake
  This distinction drives the weight allocation in Tier 1 below.

==========================================================================
TIER 1 -- HIGH CONFIDENCE  (Real PMIU Visit + Synthetic rows)
==========================================================================
  These rows have full monitoring-visit data including physical attendance
  headcount, teacher presence, and toilet functionality.

  Component                           Weight   Rationale
  ------------------------------------------------------------------
  Attendance_Rate_Percent (inverted)   30%     Highest weight.  Physical student head-
                                               count on visit day is the single hardest
                                               metric to fabricate and the defining
                                               trait of a ghost school.  Aligned with
                                               the World Bank SDI emphasis on absence.
  Building_Illegal_Occupation          15%     A confirmed "Yes" is a direct fraud
                                               signal (building used as residence,
                                               tuition academy, etc.).  Plus a
                                               priority-escalation rule on top.
  Functional_Toilet_Rate (inverted)    15%     Non-functional toilets signal
                                               infrastructure neglect that commonly
                                               accompanies ghost-school profiles.
  Boundary_Wall = "No"                 10%     Missing boundary wall = unsecured
                                               premises vulnerable to encroachment.
  Drinking_Water = "No"                10%     No drinking water strongly suggests
                                               the school does not serve students
                                               on a daily basis.
  Monitoring_Recency                   10%     Days since last monitoring visit;
                                               linear 0-365 day scale capped at 365.
                                               Schools not recently audited are
                                               harder to hold accountable and
                                               accumulated risk is unverified.
  Electricity = "No"                    5%     Lower weight because many rural
                                               primary schools legitimately operate
                                               without grid electricity.
  Fill_Rate_Percent (inverted)          5%     Intentionally the lowest weight.
                                               Teacher vacancy is a PAPER metric
                                               that is trivially gamed -- ghost
                                               teachers can be "filled" in records
                                               with no one physically present.
                                               ------------------------------------------------------------------
                                     Total:   100%

  DELIBERATELY EXCLUDED -- Enrollment-ratio / students-per-teacher signal:
    Pakistan's national primary pupil-teacher ratio averages 60-80:1 and is
    documented up to 200:1 in genuine overcrowding cases.  No fixed threshold
    reliably separates real overcrowding from enrollment-record fraud, so this
    signal is excluded to avoid false positives.

==========================================================================
TIER 2 -- SCREENING ONLY  (Real Census rows)
==========================================================================
  Census rows (2017-18 Annual School Census) have ONLY infrastructure data:
  boundary wall, electricity, drinking water, toilets, and building-
  occupation status.  There is NO attendance headcount and NO teacher-
  presence data because the census was not a monitoring visit.

  This tier is therefore a LOWER-CONFIDENCE, infrastructure-only screening
  signal -- NOT a full ghost-school risk assessment.  Schools flagged here
  should be prioritised for satellite verification and/or a physical
  monitoring visit before any audit action.

  Component                           Weight   Rationale
  ------------------------------------------------------------------
  Building_Illegal_Occupation          35%     With no attendance data, a confirmed
                                               illegal occupation is the single
                                               strongest available fraud signal.
  Functional_Toilet_Rate (inverted)    25%     Infrastructure neglect proxy.
  Boundary_Wall = "No"                 15%     Unsecured premises.
  Drinking_Water = "No"                15%     School may not serve students daily.
  Electricity = "No"                   10%     Lower weight (rural schools often
                                               lack grid power legitimately).
                                               ------------------------------------------------------------------
                                     Total:   100%

==========================================================================
PRIORITY THRESHOLDS (fixed, global -- identical for BOTH tiers):
  High   : score >= 50   (strong indicators, immediate audit / visit)
  Medium : score >= 20   (moderate risk, schedule for review)
  Low    : score <  20   (appears functional or no strong signal)

  Illegal-occupation escalation: a school with Building_Illegal_Occupation
  = "Yes" has its Priority bumped up by one level (Low->Medium, Medium->High)
  AFTER the score-based assignment.  The score itself is NOT modified.
"""

import pandas as pd
import numpy as np
from datetime import datetime
import sys
import os

# ---------------------------------------------------------------------------
# Force UTF-8 output on Windows to avoid cp1252 encoding errors
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_CSV = os.path.join(SCRIPT_DIR, "ghost_school_RAW_dataset_VERIFIED (1).csv")
OUTPUT_CSV = os.path.join(SCRIPT_DIR, "ghostwatch_two_tier_scores.csv")
TODAY = datetime.now()
RECENCY_CAP_DAYS = 365  # anything above this gets full recency risk points

# Synthetic-row ground-truth expectations (for validation)
SYNTHETIC_EXPECTATIONS = {
    99990001: {"expect": "High",   "label": "Ghost school"},
    99990002: {"expect": "High",   "label": "Ghost school"},
    99990003: {"expect": "High",   "label": "Ghost school (ghost teachers)"},
    99990004: {"expect": "High",   "label": "Edge: illegal occupation (escalated)"},
    99990005: {"expect": "Low",    "label": "Edge: clean metrics, no enrollment signal"},
    99990006: {"expect": "High",   "label": "Ghost school"},
    99990007: {"expect": "Low",    "label": "True-negative control"},
}


# ---------------------------------------------------------------------------
# 1. Read data
# ---------------------------------------------------------------------------
df = pd.read_csv(INPUT_CSV)
print(f"Loaded {len(df)} rows from '{os.path.basename(INPUT_CSV)}'")
print(f"  Data_Type breakdown: "
      f"{(df['Data_Type'] == 'Real (PMIU Visit)').sum()} PMIU Visit, "
      f"{(df['Data_Type'] == 'Synthetic').sum()} Synthetic, "
      f"{(df['Data_Type'] == 'Real (Census)').sum()} Census\n")


# ---------------------------------------------------------------------------
# 2. Parse Monitoring_Date for every row
# ---------------------------------------------------------------------------
def parse_monitoring_date(raw):
    """
    PMIU/Synthetic dates: 'DD-MM-YYYY'
    Census dates:         'Oct-2018 (Annual Census)'  -> extract month+year
    """
    if pd.isna(raw):
        return pd.NaT
    raw = str(raw).strip()
    # Try DD-MM-YYYY first
    try:
        return datetime.strptime(raw, "%d-%m-%Y")
    except ValueError:
        pass
    # Census format: "Mon-YYYY (Annual Census)"
    try:
        prefix = raw.split("(")[0].strip()   # "Oct-2018"
        return datetime.strptime(prefix, "%b-%Y")
    except (ValueError, IndexError):
        return pd.NaT


df["_mon_date"] = df["Monitoring_Date"].apply(parse_monitoring_date)
df["_days_since"] = (TODAY - df["_mon_date"]).dt.days
df.loc[df["_days_since"] < 0, "_days_since"] = 0  # future dates -> 0


# ---------------------------------------------------------------------------
# 3. Helpers: map raw fields to 0-1 risk fractions
# ---------------------------------------------------------------------------
def binary_no_risk(series):
    """'No' -> 1.0 (risky).  'Yes-Wholly'/'Yes-Partially'/other -> 0.0."""
    return (series.astype(str).str.strip().str.lower() == "no").astype(float)


def binary_yes_risk(series):
    """'Yes' -> 1.0 (risky).  'No'/other -> 0.0."""
    return (series.astype(str).str.strip().str.lower() == "yes").astype(float)


def safe_divide(num, denom):
    """Element-wise division; NaN where denominator is 0 or NaN."""
    return pd.to_numeric(num, errors="coerce") / pd.to_numeric(denom, errors="coerce").replace(0, np.nan)


def invert_pct(series):
    """Convert a 0-100 percentage to a 0-1 risk fraction (inverted).
    Low percentage -> high risk.  NaN -> 0.5 (conservative)."""
    result = 1.0 - (series.clip(0, 100) / 100.0)
    return result.fillna(0.5)


# ---------------------------------------------------------------------------
# 4. Split into tiers
# ---------------------------------------------------------------------------
tier1_mask = df["Data_Type"].isin(["Real (PMIU Visit)", "Synthetic"])
tier2_mask = df["Data_Type"] == "Real (Census)"

tier1 = df[tier1_mask].copy()
tier2 = df[tier2_mask].copy()

print(f"Tier 1 (PMIU Visit + Synthetic): {len(tier1)} rows")
print(f"Tier 2 (Census):                 {len(tier2)} rows\n")


# ===================================================================
# TIER 1 SCORING
# ===================================================================

# Derived percentages
tier1["_attendance_pct"] = safe_divide(tier1["Total_Present"], tier1["Total_Enrolled"]) * 100
tier1["_fill_pct"]       = safe_divide(tier1["Teachers_Filled"], tier1["Teachers_Sanctioned"]) * 100
tier1["_toilet_pct"]     = safe_divide(tier1["Toilets_Functional"], tier1["Toilets_Total"]) * 100

# Risk fractions (each 0-1)
tier1["r_attendance"]     = invert_pct(tier1["_attendance_pct"])
tier1["r_fill"]           = invert_pct(tier1["_fill_pct"])
tier1["r_toilet"]         = invert_pct(tier1["_toilet_pct"])
tier1["r_illegal"]        = binary_yes_risk(tier1["Building_Illegal_Occupation"])
tier1["r_boundary"]       = binary_no_risk(tier1["Boundary_Wall"])
tier1["r_water"]          = binary_no_risk(tier1["Drinking_Water"])
tier1["r_electricity"]    = binary_no_risk(tier1["Electricity"])
tier1["r_recency"]        = (tier1["_days_since"].clip(upper=RECENCY_CAP_DAYS)
                             / RECENCY_CAP_DAYS)

# Weighted sum  (weights are percentages that sum to 100)
TIER1_WEIGHTS = {
    "r_attendance":   30,
    "r_illegal":      15,
    "r_toilet":       15,
    "r_boundary":     10,
    "r_water":        10,
    "r_recency":      10,
    "r_electricity":   5,
    "r_fill":          5,
}

tier1["Ghost_Risk_Score_Tier1"] = sum(
    tier1[col] * w for col, w in TIER1_WEIGHTS.items()
).round(2)

tier1["Tier"] = 1
tier1["Confidence_Level"] = "High"


# ===================================================================
# TIER 2 SCORING  (infrastructure only -- no attendance/teacher data)
# ===================================================================

# Derived toilet percentage
tier2["_toilet_pct"] = safe_divide(tier2["Toilets_Functional"], tier2["Toilets_Total"]) * 100

# Risk fractions
tier2["r_toilet"]       = invert_pct(tier2["_toilet_pct"])
tier2["r_illegal"]      = binary_yes_risk(tier2["Building_Illegal_Occupation"])
tier2["r_boundary"]     = binary_no_risk(tier2["Boundary_Wall"])
# For census rows, treat NaN electricity/water as "No" (unknown = conservative)
tier2["r_water"]        = binary_no_risk(tier2["Drinking_Water"].fillna("No"))
tier2["r_electricity"]  = binary_no_risk(tier2["Electricity"].fillna("No"))

TIER2_WEIGHTS = {
    "r_illegal":      35,
    "r_toilet":       25,
    "r_boundary":     15,
    "r_water":        15,
    "r_electricity":  10,
}

tier2["Infrastructure_Screening_Score"] = sum(
    tier2[col] * w for col, w in TIER2_WEIGHTS.items()
).round(2)

tier2["Tier"] = 2
tier2["Confidence_Level"] = "Low - Needs Satellite Verification"


# ===================================================================
# PRIORITY + ESCALATION  (both tiers)
# ===================================================================

def score_to_priority(score):
    """Fixed global thresholds -- identical for every row."""
    if pd.isna(score):
        return "Unknown"
    if score >= 50:
        return "High"
    elif score >= 20:
        return "Medium"
    return "Low"


# Compute score-based priority from the appropriate tier score
tier1["_score"] = tier1["Ghost_Risk_Score_Tier1"]
tier2["_score"] = tier2["Infrastructure_Screening_Score"]

tier1["Priority"] = tier1["_score"].apply(score_to_priority)
tier2["Priority"] = tier2["_score"].apply(score_to_priority)

# Illegal-occupation escalation: bump priority up one level
ESCALATION = {"Low": "Medium", "Medium": "High", "High": "High"}

for frame in (tier1, tier2):
    illegal = frame["r_illegal"] == 1.0
    frame.loc[illegal, "Priority"] = frame.loc[illegal, "Priority"].map(ESCALATION)


# ===================================================================
# COMBINE & SAVE
# ===================================================================
combined = pd.concat([tier1, tier2], ignore_index=True)

# Build clean output columns (drop internal helper columns)
score_col_t1 = "Ghost_Risk_Score_Tier1"
score_col_t2 = "Infrastructure_Screening_Score"

# Ensure both score columns exist on every row (NaN where not applicable)
combined[score_col_t1] = combined.get(score_col_t1, np.nan)
combined[score_col_t2] = combined.get(score_col_t2, np.nan)

drop_cols = [c for c in combined.columns if c.startswith("r_") or c.startswith("_")]
combined = combined.drop(columns=drop_cols)

combined.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
print(f"Saved {len(combined)} rows to '{os.path.basename(OUTPUT_CSV)}'\n")


# ===================================================================
# REPORTING
# ===================================================================
SEP_HEAVY = "=" * 100
SEP_LIGHT = "-" * 100


# --- Section 1: Tier counts ---
print(SEP_HEAVY)
print("  1. TIER COUNTS")
print(SEP_HEAVY)
for tier_label, mask in [("Tier 1 (High Confidence)", tier1_mask),
                          ("Tier 2 (Screening Only)",  tier2_mask)]:
    subset = combined[mask]
    counts = subset["Priority"].value_counts()
    hi = counts.get("High", 0)
    md = counts.get("Medium", 0)
    lo = counts.get("Low", 0)
    print(f"  {tier_label}: {len(subset):>3} schools  "
          f"(High: {hi}, Medium: {md}, Low: {lo})")
print()


# --- Section 2: All High / Medium schools, grouped by tier ---
print(SEP_HEAVY)
print("  2. SCHOOLS FLAGGED HIGH OR MEDIUM PRIORITY  (full list)")
print(SEP_HEAVY)

for tier_num, tier_label, score_col in [
    (1, "TIER 1 -- High Confidence (PMIU Visit + Synthetic)", "Ghost_Risk_Score_Tier1"),
    (2, "TIER 2 -- Screening Only (Census)", "Infrastructure_Screening_Score"),
]:
    tier_data = combined[combined["Tier"] == tier_num]
    flagged = tier_data[tier_data["Priority"].isin(["High", "Medium"])].copy()
    flagged = flagged.sort_values(["Priority", score_col],
                                  ascending=[True, False],
                                  na_position="last")

    hi_count = (flagged["Priority"] == "High").sum()
    md_count = (flagged["Priority"] == "Medium").sum()

    print(f"\n  {tier_label}")
    print(f"  High: {hi_count}  |  Medium: {md_count}  |  Total flagged: {len(flagged)}")
    print(SEP_LIGHT)

    if flagged.empty:
        print("    (none)")
    else:
        # Header
        print(f"  {'Pri':<8} {'Score':>7}  {'School_Name':<50} {'District':<20}")
        print(f"  {'---':<8} {'-----':>7}  {'-' * 50:<50} {'-' * 20:<20}")
        for _, row in flagged.iterrows():
            prio = row["Priority"]
            score = row[score_col]
            name = str(row["School_Name"])[:50]
            district = str(row["District"])[:20]
            score_str = f"{score:7.2f}" if pd.notna(score) else "    N/A"
            print(f"  {prio:<8} {score_str}  {name:<50} {district:<20}")

print()


# --- Section 3: Synthetic validation ---
print(SEP_HEAVY)
print("  3. SYNTHETIC VALIDATION  (Tier 1 rows only)")
print(SEP_HEAVY)
print(f"  {'EMIS':<12} {'Score':>7} {'Pri':<8} {'Esc?':<5} "
      f"{'Expected':<10} {'Status':<8} Label")
print(f"  {'----':<12} {'-----':>7} {'---':<8} {'----':<5} "
      f"{'--------':<10} {'------':<8} -----")

synth = combined[combined["Data_Type"] == "Synthetic"].copy()
correct = 0
total = 0

for _, row in synth.iterrows():
    emis = int(row["EMIS_Code"])
    score = row["Ghost_Risk_Score_Tier1"]
    prio = row["Priority"]
    info = SYNTHETIC_EXPECTATIONS.get(emis, {"expect": "?", "label": "Unknown"})
    expected = info["expect"]
    label = info["label"]

    # Was illegal-occupation escalation applied?
    illegal_flag = str(row.get("Building_Illegal_Occupation", "")).strip().lower() == "yes"
    esc_str = "Yes" if illegal_flag else ""

    # Determine pass/fail
    passed = prio == expected
    status = "[PASS]" if passed else "[FAIL]"
    if passed:
        correct += 1
    total += 1

    print(f"  {emis:<12} {score:7.2f} {prio:<8} {esc_str:<5} "
          f"{expected:<10} {status:<8} {label}")

print(SEP_LIGHT)
print(f"  Result: {correct}/{total} synthetic cases scored correctly.")
if correct == total:
    print("  All validation cases pass.")
else:
    failed = total - correct
    print(f"  WARNING: {failed} case(s) did not match expectations.")
print(SEP_HEAVY)
print("\nDone.")
