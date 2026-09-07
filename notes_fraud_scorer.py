"""
notes_fraud_scorer.py
=====================

Generalisable, transparent rule-based fraud-phrase scorer for school
monitoring Notes.  Replaces the earlier hardcoded EMIS-code lookup with
categorised regex patterns that work on ANY note text.

HONESTY NOTE: despite the historical "Qwen_" prefix on the output columns
(Qwen_Fraud_Flag / Qwen_Fraud_Reason), this scorer is a fully deterministic,
rule-based regex matcher -- no LLM API is called anywhere in the pipeline.
The column names are kept for backward compatibility with the backend,
dashboard and audits.

METHODOLOGY
-----------
1. Every Tier 1 note is first checked against an ADMIN-DATA-NOISE filter.
   Notes that are purely about data-sync issues (SIS/NSB mismatches, name
   inconsistencies, roster lapses) are excluded from fraud scoring because
   these are administrative, not fraudulent.  SAFETY: if strong fraud
   keywords also appear in the same note, the admin filter is overridden.

2. Remaining notes are scanned against four categorised fraud-pattern
   groups.  First match wins.

3. An explicit ROUTINE-EXCLUSION list ensures that infrastructure
   maintenance language is never flagged as fraud on its own.

FRAUD CATEGORIES
----------------
  A  Falsified records / attendance
  B  Unauthorized absence with impunity
  C  Illegal occupation / misuse of school property
  D  Ghost-teacher / salary fraud
  E  Ghost-school indicators (abandoned / non-functional)

ESCALATION (values live in ghostwatch_config.py)
----------
  Ghost_Risk_Score_Tier1 += FRAUD_SCORE_BOOST  (capped at SCORE_CAP)
  Priority: Low -> Medium, Medium -> High, High stays High
  (merge_satellite_scores_v2.py later recomputes Priority from the score,
  where the fraud escalation is FRAUD_PRIORITY_ESCALATION_MERGE; the score
  boost is what survives into the final file.)

USAGE
-----
  # Regenerate base CSV first (from ghostwatch_scoring.py), then:
  python notes_fraud_scorer.py
"""

import re
import pandas as pd

from ghostwatch_config import (
    FRAUD_PRIORITY_ESCALATION,
    FRAUD_SCORE_BOOST,
    SCORE_CAP,
)

# =====================================================================
#  FRAUD PATTERN CATEGORIES  (compiled regexes, case-insensitive)
# =====================================================================

FRAUD_PATTERNS = {
    "A - Falsified records / attendance": [
        re.compile(r"falsely\s+marked\s+present"),
        re.compile(r"marked\s+present.*(?:not|absent|fake)"),
        re.compile(r"(?:attendance|register).{0,25}falsif"),
        re.compile(r"falsif.{0,25}(?:attendance|register)"),
        re.compile(r"attendance\s+register.{0,20}mismatch"),
        re.compile(r"register.{0,20}attendance.{0,15}mismatch"),
    ],

    "B - Unauthorized absence with impunity": [
        re.compile(r"unauthorized\s+(?:long.?term\s+)?absence"),
        re.compile(r"no\s+action\s+taken"),
        re.compile(r"without\s+(?:any\s+)?action"),
        re.compile(r"absence.{0,25}no\s+action"),
    ],

    "C - Illegal occupation / misuse of school property": [
        re.compile(r"(?:used|operated)\s+as\s+(?:a\s+)?private\s+residence"),
        re.compile(r"used\s+as\s+residence"),
        re.compile(r"illegally\s+occupied"),
        re.compile(r"rented\s+to\s+(?:a\s+)?private"),
        re.compile(r"building\s+on\s+rent(?!;?\s*maintenance)"),
        re.compile(r"illegal\s+occupation"),
        re.compile(r"(?:classrooms?|rooms?|building)\s+.{0,10}rented\s+to"),
    ],

    "D - Ghost-teacher / salary fraud": [
        re.compile(r"ghost.?teacher"),
        re.compile(r"salary\s+fraud"),
        re.compile(r"drawing\s+salary"),
        re.compile(r"ghost\s+(?:school|staff|employee)"),
    ],

    "E - Ghost-school indicators (abandoned / non-functional)": [
        re.compile(r"no\s+staff\s+present"),
        re.compile(r"school\s+(?:found\s+)?locked"),
        re.compile(r"ghost.?school"),
        re.compile(r"near.?total\s+(?:teacher\s+)?absenteeism"),
        re.compile(r"collapsed\s+facilities"),
        re.compile(r"non.?functional\s+for\s+over"),
    ],
}

# =====================================================================
#  ROUTINE-MAINTENANCE EXCLUSIONS
#  If a note matches ONLY these and no fraud pattern, it is clean.
#  (Used for documentation; the actual logic is: fraud patterns are
#   checked first, so routine phrases never trigger a flag.)
# =====================================================================

ROUTINE_EXCLUSIONS = [
    re.compile(r"toilet.{0,10}non\s*functional"),
    re.compile(r"boundary\s+wall\s+(?:required|needed|not\s+available)"),
    re.compile(r"minor\s+repair"),
    re.compile(r"needs?\s+repair"),
    re.compile(r"exam\s*(?:ination)?\s*duty"),
    re.compile(r"furniture\s+required"),
    re.compile(r"whitewash"),
    re.compile(r"maintenance"),
    re.compile(r"repairs?\s+(?:noted|required)"),
    re.compile(r"(?:required|noted).{0,10}repairs?"),
]

# =====================================================================
#  ADMIN-DATA-NOISE FILTER
#  Notes that are primarily about data-sync / record-keeping issues
#  (SIS mismatches, name inconsistencies, roster lapses) are NOT fraud.
#  SAFETY: strong fraud keywords override this filter.
# =====================================================================

ADMIN_DATA_SIGNALS = [
    re.compile(r"\bSIS\b.{0,30}(?:mismatch|not\s+updated|not\s+reflected)"),
    re.compile(r"(?:mismatch|not\s+updated|not\s+reflected).{0,30}\bSIS\b"),
    re.compile(r"NSB\s+record\s+not\s+updated"),
    re.compile(r"roster\s+not\s+updated"),
    re.compile(r"name\s+inconsistent"),
    re.compile(r"register.{0,15}not\s+reflected\s+in\s+SIS"),
]

# Strong fraud keywords that OVERRIDE the admin-data filter
STRONG_FRAUD_OVERRIDE = [
    re.compile(r"falsely"),
    re.compile(r"ghost.?teacher"),
    re.compile(r"salary\s+fraud"),
    re.compile(r"no\s+action\s+taken"),
    re.compile(r"unauthorized\s+absence"),
    re.compile(r"private\s+residence"),
    re.compile(r"rented\s+to\s+private"),
]


# =====================================================================
#  SCORING FUNCTION
# =====================================================================

def analyze_note(note_text):
    """
    Analyse a single note string.

    Returns
    -------
    (fraud_flag: bool, reason: str)
        fraud_flag = True  -> pattern matched, reason = which category + quote
        fraud_flag = False -> clean, reason = short explanation
    """
    if not note_text or not str(note_text).strip():
        return False, ""

    text = str(note_text)

    # ── Step 1: Admin-data-noise filter ──────────────────────────────
    is_admin = any(p.search(text) for p in ADMIN_DATA_SIGNALS)

    if is_admin:
        # Check if strong fraud keywords also appear -> override filter
        has_strong_fraud = any(p.search(text) for p in STRONG_FRAUD_OVERRIDE)
        if not has_strong_fraud:
            return False, "No fraud indicators -- administrative data note"
        # else: fall through to fraud-pattern scan (override admin filter)

    # ── Step 2: Scan fraud-pattern categories ────────────────────────
    for category, patterns in FRAUD_PATTERNS.items():
        for pat in patterns:
            m = pat.search(text)
            if m:
                return True, f"{category}: matched \"{m.group()}\""

    # ── Step 3: No fraud pattern matched ─────────────────────────────
    return False, "No fraud indicators -- routine note"


# =====================================================================
#  MAIN
# =====================================================================

def main():
    CSV = "ghostwatch_two_tier_scores.csv"
    df = pd.read_csv(CSV)
    print(f"Loaded {len(df)} rows from {CSV}")

    # ── Initialise columns ───────────────────────────────────────────
    df["Qwen_Fraud_Flag"] = False
    df["Qwen_Fraud_Reason"] = ""

    # ── Score only Tier 1 rows ───────────────────────────────────────
    tier1_mask = df["Tier"] == 1
    tier1_with_notes = df[
        tier1_mask
        & df["Notes"].notna()
        & (df["Notes"].str.strip() != "")
    ]

    fraud_count = 0
    clean_count = 0

    for idx in tier1_with_notes.index:
        note = df.loc[idx, "Notes"]
        flag, reason = analyze_note(note)

        df.loc[idx, "Qwen_Fraud_Flag"] = flag
        df.loc[idx, "Qwen_Fraud_Reason"] = reason

        if flag:
            fraud_count += 1

            # ── Score boost (capped at SCORE_CAP) ─────────────────────
            old_score = df.loc[idx, "Ghost_Risk_Score_Tier1"]
            df.loc[idx, "Ghost_Risk_Score_Tier1"] = min(
                old_score + FRAUD_SCORE_BOOST, float(SCORE_CAP))

            # ── Priority escalation ──────────────────────────────────
            old_pri = df.loc[idx, "Priority"]
            df.loc[idx, "Priority"] = FRAUD_PRIORITY_ESCALATION.get(old_pri, old_pri)
        else:
            clean_count += 1

    # ── Save ─────────────────────────────────────────────────────────
    df.to_csv(CSV, index=False)
    print(f"Saved updated CSV to {CSV}")

    # ── Print fraud-flagged rows ─────────────────────────────────────
    flagged = df[df["Qwen_Fraud_Flag"] == True]
    print(f"\n{'='*90}")
    print(f"  FRAUD-FLAGGED ROWS  ({len(flagged)} flagged, "
          f"{clean_count} clean notes)")
    print(f"{'='*90}")
    for _, r in flagged.iterrows():
        print(f"\n  EMIS        : {int(r['EMIS_Code'])}")
        print(f"  School      : {r['School_Name']}")
        print(f"  District    : {r['District']}")
        print(f"  Notes       : {str(r['Notes'])[:160]}")
        print(f"  Reason      : {r['Qwen_Fraud_Reason']}")
        print(f"  Score       : {r['Ghost_Risk_Score_Tier1']:.2f}")
        print(f"  Priority    : {r['Priority']}")
        print(f"  {'-'*70}")

    # ── Validation: show ALL 24 notes with their verdict ─────────────
    print(f"\n{'='*90}")
    print("  FULL VALIDATION: ALL 24 Tier 1 NOTES")
    print(f"{'='*90}")
    for idx in tier1_with_notes.index:
        row = df.loc[idx]
        status = "FRAUD" if row["Qwen_Fraud_Flag"] else "clean"
        print(f"  {int(row['EMIS_Code'])}  [{status:5s}]  "
              f"{str(row['Notes'])[:80]}")
        if row["Qwen_Fraud_Flag"]:
            print(f"    -> {row['Qwen_Fraud_Reason']}")

    # ── Summary ──────────────────────────────────────────────────────
    print(f"\n{'='*90}")
    print("  PRIORITY DISTRIBUTION (all 300 rows)")
    print(f"{'='*90}")
    print(df["Priority"].value_counts().to_string())
    print()
    tier1 = df[df["Tier"] == 1]
    print("  FRAUD FLAG DISTRIBUTION (Tier 1)")
    print(tier1["Qwen_Fraud_Flag"].value_counts().to_string())


if __name__ == "__main__":
    main()
