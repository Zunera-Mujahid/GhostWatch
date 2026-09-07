"""
ghostwatch_config.py
====================
Single source of truth for every scoring / classification constant shared
across the GhostWatch pipeline.

Why this exists
---------------
The Priority thresholds, tier weights, satellite modifiers and escalation
rules used to be copy-pasted into each script.  Two copies drifting apart
silently changes a school's risk classification, so every shared value now
lives here and each pipeline script imports it.

Consumed by
-----------
  ghostwatch_scoring.py          tier weights, status override, priority
                                 thresholds, illegal-occupation escalation
  notes_fraud_scorer.py          fraud score boost + priority escalation
  merge_satellite_scores_v2.py   satellite modifiers, priority thresholds,
                                 escalation rules, location-verification floor
  add_explanation_columns.py     weights + modifiers (recomputation/validation)
  build_final_reason_and_map.py  satellite epoch years (built-up trend wording)

One-off repair / geocoding utilities (fix_duplicate_coords.py,
geoapify_fallback_geocode.py, build_risk_model.py) still carry their own
copies; they are historical and not part of the re-runnable chain.

CHANGE PROTOCOL -- editing any value below changes scores or classifications
----------------------------------------------------------------------------
1. Update the frozen snapshot in tests/test_config_guard.py so the change is
   recorded as deliberate (the guard test fails otherwise).
2. Re-run the full deterministic chain from the raw dataset:
       python ghostwatch_scoring.py         (base two-tier scores)
       python notes_fraud_scorer.py         (rule-based fraud flags + boost)
       python merge_satellite_scores_v2.py
       python add_explanation_columns.py
       python build_final_reason_and_map.py
   NOTE: ghostwatch_scoring.py rewrites ghostwatch_two_tier_scores.csv
   WITHOUT the Qwen fraud columns -- notes_fraud_scorer.py must always run
   immediately after it.
3. Re-run the audits:
       python tests/test_config_guard.py
       python tests/audit_edges.py
       python tests/audit_crosscheck.py      (needs the backend running)
       python tests/verify_lofloor.py
       python tests/audit_jscheck.py
"""

# ---------------------------------------------------------------------------
# Priority thresholds (global, identical for both tiers)
# ---------------------------------------------------------------------------
PRIORITY_HIGH_MIN = 50      # score >= 50  -> High   (immediate audit / visit)
PRIORITY_MEDIUM_MIN = 20    # score >= 20  -> Medium (schedule review); else Low

# ---------------------------------------------------------------------------
# Hard score cap
# ---------------------------------------------------------------------------
SCORE_CAP = 100

# ---------------------------------------------------------------------------
# Tier 1 weights (PMIU visits + synthetic rows; factor -> weight, sums to 100)
# Attendance carries the highest weight per the World Bank SDI emphasis on
# physical presence (paper metrics are easily gamed).
# ---------------------------------------------------------------------------
TIER1_WEIGHTS = {
    "attendance":  30,   # visit-day headcount: hardest metric to fabricate
    "illegal":     15,   # confirmed illegal occupation = direct fraud signal
    "toilet":      15,   # infrastructure neglect proxy
    "boundary":    10,   # unsecured premises
    "water":       10,   # school may not serve students daily
    "recency":     10,   # days since last monitoring visit (capped below)
    "electricity":  5,   # rural schools often lack grid power legitimately
    "fill":         5,   # paper metric, trivially gamed -- lowest weight
}

# ---------------------------------------------------------------------------
# Tier 2 weights (census infrastructure screening only, sums to 100)
# ---------------------------------------------------------------------------
TIER2_WEIGHTS = {
    "illegal":     35,   # strongest available fraud signal without attendance
    "toilet":      25,
    "boundary":    15,
    "water":       15,
    "electricity": 10,
}

# ---------------------------------------------------------------------------
# Monitoring recency
# ---------------------------------------------------------------------------
RECENCY_CAP_DAYS = 365   # linear 0-365 day scale; beyond this, full points

# ---------------------------------------------------------------------------
# School_Status override (both tiers)
# An explicit Non-Functional / Closed government declaration is the strongest
# single signal available: +STATUS_OVERRIDE_BONUS points (capped) and Priority
# forced to at least High.
# ---------------------------------------------------------------------------
STATUS_OVERRIDE_STATES = ("Non-Functional", "Closed")
STATUS_OVERRIDE_BONUS = 30

# ---------------------------------------------------------------------------
# Fraud-note escalation (notes_fraud_scorer.py, Tier 1 only)
# Applied when the rule-based note screening matches a fraud pattern.
# NOTE: merge_satellite_scores_v2.py later RECOMPUTES Priority from the score,
# so the escalation applied there is FRAUD_PRIORITY_ESCALATION_MERGE; the
# score boost written here is what survives into the final file.
# ---------------------------------------------------------------------------
FRAUD_SCORE_BOOST = 10
FRAUD_PRIORITY_ESCALATION = {"Low": "Medium", "Medium": "High", "High": "High"}

# ---------------------------------------------------------------------------
# Illegal-occupation priority escalation (bumps priority up one level; the
# score itself is never modified)
# ---------------------------------------------------------------------------
ILLEGAL_PRIORITY_ESCALATION = {"Low": "Medium", "Medium": "High", "High": "High"}

# ---------------------------------------------------------------------------
# Confidence-aware satellite modifiers (merge_satellite_scores_v2.py)
# Tehsil-centroid coordinates never represent the school's actual location,
# so they contribute ZERO points in every case (imagery there is visual
# context only).
# ---------------------------------------------------------------------------
SAT_MOD_SCHOOL = {"Low_Density": 15, "Mixed_Density": 7, "High_Density": 0, "NoData": 0}
SAT_MOD_VILLAGE = {"Low_Density": 5, "Mixed_Density": 2, "High_Density": 0, "NoData": 0}
SAT_MOD_TEHSIL = {"Low_Density": 0, "Mixed_Density": 0, "High_Density": 0, "NoData": 0}

# ---------------------------------------------------------------------------
# Fraud escalation as applied during the final merge recompute
# (Medium -> High only; Low is left to the score, which already carries the
# +FRAUD_SCORE_BOOST points)
# ---------------------------------------------------------------------------
FRAUD_PRIORITY_ESCALATION_MERGE = {"Low": "Low", "Medium": "High", "High": "High"}

# ---------------------------------------------------------------------------
# Location-verification floor (merge_satellite_scores_v2.py)
# A School-Level (exact-coordinate) school in a Low_Density area: imagery
# found almost no built-up at the claimed location, so the location is
# unverified -- Priority is floored at Medium.  Scores are untouched.
# ---------------------------------------------------------------------------
SAT_LOC_FLOOR_PRIORITY = {"Low": "Medium", "Medium": "Medium", "High": "High"}

# ---------------------------------------------------------------------------
# Satellite built-up epochs (years of the LULC layers compared by the grid
# sampling step).  Used by build_final_reason_and_map.py when the final
# reason cites a built-up change over time.
# ---------------------------------------------------------------------------
SATELLITE_EPOCH_YEARS = (2017, 2023)   # (earliest, recent)
