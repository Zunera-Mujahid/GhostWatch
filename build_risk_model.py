"""
build_risk_model.py

Builds a transparent risk-scoring model for ghost-school detection using only
raw monitoring fields. Completely ignores the pre-existing placeholder columns
(Government_Monitoring_Risk_Score and Audit_Priority) as inputs.

WEIGHTING RATIONALE (total = 100 points):
-------------------------------------------
The scoring formula assigns risk points so that WORSE conditions produce HIGHER
scores (0 = perfect school, 100 = confirmed ghost school).

  Component                          Max Points   Why this weight?
  ---------------------------------------------------------------
  Fill_Rate_Percent (low = risky)         4       Teacher vacancy is a paper metric
                                                  that is easily gamed (ghost teachers
                                                  can be "filled" on paper with no one
                                                  physically present).  Minor signal.
  Attendance_Rate_Percent (low = risky)  28       Physical headcount on visit day is
                                                  the HARDEST metric to fake and the
                                                  defining trait of a ghost school.
  Building_Illegal_Occupation (Yes)      25       Illegal occupation (building used
                                                  as private residence, tuition
                                                  academy, etc.) is one of the
                                                  strongest fraud signals available.
                                                  A school can have perfect attendance
                                                  numbers yet still be misused -- this
                                                  weight catches that on its own.
  Functional_Toilet_Rate_Percent          8       Broken/missing toilets signal
  (low = risky)                                 infrastructure neglect.
  Boundary_Wall (absent/partial)          7       Unsecured premises are vulnerable
                                                  to encroachment.
  Drinking_Water (absent/partial)         7       Lack of drinking water indicates
                                                  the school may not actually serve
                                                  students on a daily basis.
  Electricity (absent/partial)            6       Lower weight; many rural primary
                                                  schools operate without grid power.
  Enrollment_Implausibility (derived)    15       Flags schools where Total_Enrolled
                                                  is implausibly high relative to
                                                  Teachers_Filled or Toilets_Total
                                                  (e.g. 210 students with 3 teachers
                                                  and 1 toilet).  Derived from raw
                                                  fields, not an independent input.
                                                  Higher weight compensates for the
                                                  fact that this is a binary trigger
                                                  that fires only when ratios are
                                                  egregiously out of range.
                                                  -------
                                          Total:  100

ENROLLMENT IMPLAUSIBILITY RULE:
  students_per_teacher = Total_Enrolled / max(Teachers_Filled, 1)
  students_per_toilet  = Total_Enrolled / max(Toilets_Functional, 1)
  If students_per_teacher > 50 OR students_per_toilet > 150 the school
  receives the full enrollment-implausibility risk points.
  Rationale: Pakistani government norms cap primary-school class size at
  ~40-50 students per teacher; ratios far above that suggest inflated
  enrollment records (a hallmark of ghost schools).

PRIORITY THRESHOLDS (fixed, global -- identical for every row):
  High   : score >= 50   (strong ghost-school indicators, immediate audit)
  Medium : score >= 15   (moderate risk, schedule for review)
  Low    : score <  15   (appears functional, routine monitoring)
"""

import pandas as pd
import sys
import os

# ---------------------------------------------------------------------------
# Force UTF-8 output on Windows so printed tables don't crash on cp1252
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

INPUT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "ghost_school_detector_full_dataset.csv")
OUTPUT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "schools_with_computed_scores.csv")

# ---------------------------------------------------------------------------
# 1. Read data
# ---------------------------------------------------------------------------
df = pd.read_csv(INPUT_CSV)
print(f"Loaded {len(df)} rows from '{os.path.basename(INPUT_CSV)}'\n")

# Stash reference columns (NOT used as inputs to the model)
reference_cols = ["Government_Monitoring_Risk_Score", "Audit_Priority"]
df_reference = df[reference_cols].copy()

# ---------------------------------------------------------------------------
# 2. Helper: normalise raw fields into 0-1 risk fractions
#    (0 = best condition, 1 = worst condition)
# ---------------------------------------------------------------------------

def fill_rate_risk(series: pd.Series) -> pd.Series:
    """Low fill rate -> high risk.  0% fill = 1.0 risk, 100% fill = 0.0 risk."""
    return 1.0 - (series.clip(0, 100) / 100.0)


def attendance_risk(series: pd.Series) -> pd.Series:
    """Low attendance -> high risk.  0% attendance = 1.0 risk, 100% = 0.0 risk."""
    return 1.0 - (series.clip(0, 100) / 100.0)


def toilet_risk(series: pd.Series) -> pd.Series:
    """Low functional-toilet rate -> high risk."""
    return 1.0 - (series.clip(0, 100) / 100.0)


def binary_yes_risk(series: pd.Series) -> pd.Series:
    """'Yes' -> 1.0 (risky), 'No' -> 0.0.  Case-insensitive."""
    return series.astype(str).str.strip().str.lower().map(
        {"yes": 1.0, "no": 0.0}
    ).fillna(0.0)


def categorical_facility_risk(series: pd.Series) -> pd.Series:
    """
    Map facility columns that use 'Yes-Wholly', 'Yes-Partially', 'No' to a
    0-1 risk scale:
        'Yes-Wholly'    -> 0.0  (fully available, no risk)
        'Yes-Partially' -> 0.5  (partial, moderate risk)
        'No'            -> 1.0  (absent, maximum risk)
        NaN / other     -> 0.5  (conservative default for missing data)
    """
    mapping = {
        "yes-wholly":    0.0,
        "yes-partially": 0.5,
        "no":            1.0,
    }
    normalised = series.astype(str).str.strip().str.lower()
    return normalised.map(mapping).fillna(0.5)


# ---------------------------------------------------------------------------
# 3. Compute individual risk components (each in 0-1 range)
# ---------------------------------------------------------------------------
df["risk_fill_rate"]      = fill_rate_risk(df["Fill_Rate_Percent"])
df["risk_attendance"]     = attendance_risk(df["Attendance_Rate_Percent"])
df["risk_toilet"]         = toilet_risk(df["Functional_Toilet_Rate_Percent"])
df["risk_illegal_occ"]    = binary_yes_risk(df["Building_Illegal_Occupation"])
df["risk_boundary_wall"]  = categorical_facility_risk(df["Boundary_Wall"])
df["risk_drinking_water"] = categorical_facility_risk(df["Drinking_Water"])
df["risk_electricity"]    = categorical_facility_risk(df["Electricity"])

# ---------------------------------------------------------------------------
# 3b. Derived: enrollment implausibility  (0 or 1)
# ---------------------------------------------------------------------------
# Pakistani government norms target ~40-50 students per teacher for primary
# schools.  Ratios far above that, combined with very few toilets, suggest
# enrollment records have been inflated on paper (a hallmark of ghost schools).
STUDENTS_PER_TEACHER_THRESHOLD = 50   # above this is implausible
STUDENTS_PER_TOILET_THRESHOLD  = 150  # above this is implausible

teachers_safe = df["Teachers_Filled"].clip(lower=1)   # avoid division by zero
toilets_safe  = df["Toilets_Functional"].clip(lower=1)

df["students_per_teacher"] = df["Total_Enrolled"] / teachers_safe
df["students_per_toilet"]  = df["Total_Enrolled"] / toilets_safe

df["risk_enrollment_implausible"] = (
    (df["students_per_teacher"] > STUDENTS_PER_TEACHER_THRESHOLD)
    | (df["students_per_toilet"]  > STUDENTS_PER_TOILET_THRESHOLD)
).astype(float)  # 1.0 if either ratio is implausible, else 0.0

# ---------------------------------------------------------------------------
# 4. Weighted aggregation  (weights sum to 100)
# ---------------------------------------------------------------------------
WEIGHTS = {
    # --- paper metrics (easily gamed) ---
    "risk_fill_rate":              4,
    # --- physical-presence metrics (hard to fake) ---
    "risk_attendance":            28,
    # --- binary red flag (strong standalone fraud signal) ---
    "risk_illegal_occ":           25,
    # --- infrastructure signals ---
    "risk_toilet":                 8,
    "risk_boundary_wall":          7,
    "risk_drinking_water":         7,
    "risk_electricity":            6,
    # --- derived: enrollment plausibility ---
    "risk_enrollment_implausible": 15,
}

# Each risk component is 0-1; multiply by its weight and sum.
# Result is already in 0-100 range because weights sum to 100.
df["Computed_Risk_Score"] = sum(
    df[col] * w for col, w in WEIGHTS.items()
)

# Round to 2 decimal places for readability
df["Computed_Risk_Score"] = df["Computed_Risk_Score"].round(2)

# ---------------------------------------------------------------------------
# 5. Assign priority label
# ---------------------------------------------------------------------------

def score_to_priority(score: float) -> str:
    """Fixed global thresholds -- identical for every row regardless of Data_Type."""
    if score >= 50:
        return "High"
    elif score >= 15:
        return "Medium"
    else:
        return "Low"


df["Computed_Priority"] = df["Computed_Risk_Score"].apply(score_to_priority)

# ---------------------------------------------------------------------------
# 6. Build output dataframe (drop intermediate risk_ columns)
# ---------------------------------------------------------------------------
intermediate_cols = [c for c in df.columns if c.startswith("risk_")]
intermediate_cols += ["students_per_teacher", "students_per_toilet"]
df_out = df.drop(columns=intermediate_cols)

# ---------------------------------------------------------------------------
# 7. Save to CSV
# ---------------------------------------------------------------------------
df_out.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
print(f"Saved output ({len(df_out)} rows) to '{os.path.basename(OUTPUT_CSV)}'\n")

# ---------------------------------------------------------------------------
# 8. Validation: compare Computed_Risk_Score vs known Synthetic labels
# ---------------------------------------------------------------------------
synthetic = df_out[df_out["Data_Type"] == "Synthetic"].copy()

if synthetic.empty:
    print("No Synthetic rows found -- skipping validation.")
else:
    sep = "=" * 90
    print(sep)
    print("  MODEL VALIDATION ON SYNTHETIC TEST CASES")
    print(sep)

    # The Synthetic rows carry known ground-truth in their Notes column:
    #   - Ghost schools: 99990001, 99990002, 99990003, 99990006
    #     (expected: High -- score >= 50)
    #   - Edge-case: illegal occupation: 99990004 (attendance/facilities look
    #     clean but building is misused; illegal_occ weight of 25 pushes it
    #     into Medium -- score >= 15)
    #   - Edge-case: implausible ratios: 99990005 (210 students / 3 teachers
    #     / 1 toilet; enrollment-implausibility component flags it, pushing
    #     it into Medium)
    #   - True-negative control: 99990007 (fully clean, expected: Low)

    known_ghost_emis = {"99990001", "99990002", "99990003", "99990006"}

    correct = 0
    total   = 0

    for _, row in synthetic.iterrows():
        emis   = str(row["EMIS_Code"])
        name   = row["School_Name"]
        score  = row["Computed_Risk_Score"]
        prio   = row["Computed_Priority"]
        ref_s  = row.get("Government_Monitoring_Risk_Score", "N/A")
        ref_p  = row.get("Audit_Priority", "N/A")

        # Determine expectation
        if emis in known_ghost_emis:
            expected = "High"
            label    = "Ghost school"
        elif emis == "99990004":
            expected = "Medium+"
            label    = "Edge-case: illegal occupation (score-driven)"
        elif emis == "99990005":
            expected = "Medium+"
            label    = "Edge-case: implausible enrollment ratios"
        elif emis == "99990007":
            expected = "Low"
            label    = "True-negative control"
        else:
            expected = "?"
            label    = "Unknown synthetic"

        # Evaluate correctness
        if expected == "High":
            passed = prio == "High"
        elif expected == "Medium+":
            passed = prio in ("High", "Medium")
        elif expected == "Low":
            passed = prio == "Low"
        else:
            passed = None

        status = "[PASS]" if passed else "[FAIL]" if passed is not None else "[?]"
        if passed:
            correct += 1
        if passed is not None:
            total += 1

        print(
            f"  {status}  EMIS {emis}  |  Score: {score:6.2f}  |  "
            f"Priority: {prio:<7} |  Ref: {ref_s} ({ref_p})"
        )
        print(f"         {name}")
        print(f"         Expected: {expected:<12}  Label: {label}")
        print()

    print(sep)
    print(f"  Validation summary: {correct}/{total} synthetic cases scored correctly.")
    print(sep)

print("\nDone.")
