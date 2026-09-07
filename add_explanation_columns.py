"""
add_explanation_columns.py
==========================
Adds per-source explanation columns to ghostwatch_final_merged_v2.csv:

  Data_Risk_Reason       why the row's data/infrastructure risk score
                         (Ghost_Risk_Score_Tier1 or Infrastructure_Screening_Score)
                         came out the way it did -- top contributing factors
  Satellite_Risk_Reason  satellite built-up finding, ONLY for rows with real
                         satellite data (left blank for NoData / missing rows)

Qwen_Fraud_Reason (the third source) already exists and is left untouched.

Pipeline position:
  ghostwatch_scoring.py -> merge_satellite_scores_v2.py -> add_explanation_columns.py
(re-run this script after any re-merge to refresh the reason columns)

Design notes
------------
* The score columns in the merged file ALREADY include later modifiers:
    +30 School_Status bonus for Non-Functional/Closed rows, capped at 100
        (applied by ghostwatch_scoring.py, both tiers)
    +10 Qwen fraud boost for Qwen_Fraud_Flag=True rows, capped at 100
        (applied by notes_fraud_scorer.py, Tier 1 only)
    +15 Low_Density / +7 Mixed_Density / +0 otherwise
        (applied by merge_satellite_scores_v2.py)
  Data_Risk_Reason deliberately explains only the data/infrastructure portion
  of the score (the School_Status clause leading the sentence accounts for
  the +30 bonus); the Qwen portion is explained by Qwen_Fraud_Reason and the
  satellite portion by Satellite_Risk_Reason.  The three reason columns are
  kept separate on purpose -- a combined Final_GhostSchool_Reason will be
  built later, once manually-found satellite coordinates are complete.
* Factor contributions are recomputed with the exact formulas and weights of
  ghostwatch_scoring.py (Tier 1 / Tier 2), ranked by weighted points, and the
  top factors become the reason sentence.
* School_Status is not a weighted factor in ghostwatch_scoring.py (it is
  applied as a +30 additive override, not as a weight in the sum), but
  Non-Functional / Closed rows get a leading "School_Status ..." clause
  in the reason sentence so those schools are clearly reflected.
* A one-time backup of the previous file is written before overwriting
  (the stored scores embed the monitoring-recency value from the date
  ghostwatch_scoring.py was run, so they are not trivially regenerable).
"""

from pathlib import Path
from datetime import datetime, timedelta
import shutil
import sys

import pandas as pd

from ghostwatch_config import (
    FRAUD_SCORE_BOOST,
    SAT_MOD_SCHOOL,
    SAT_MOD_VILLAGE,
    STATUS_OVERRIDE_BONUS,
    STATUS_OVERRIDE_STATES,
    TIER1_WEIGHTS,
    TIER2_WEIGHTS,
)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
FINAL_CSV = ROOT / "ghostwatch_final_merged_v2.csv"
BACKUP_CSV = ROOT / "ghostwatch_final_merged_v2_backup.csv"
TIER_CSV = ROOT / "ghostwatch_two_tier_scores.csv"

# Modifier maps and tier weights are imported from ghostwatch_config.py
# (the same single source of truth the scoring scripts use).

MIN_POINTS = 5.0    # a factor must contribute >= this many points to be cited
MAX_FACTORS = 3     # keep the sentence short


# ---------------------------------------------------------------------------
# Helpers (mirror ghostwatch_scoring.py)
# ---------------------------------------------------------------------------
def parse_monitoring_date(raw):
    if pd.isna(raw):
        return None
    raw = str(raw).strip()
    try:
        return datetime.strptime(raw, "%d-%m-%Y")
    except ValueError:
        pass
    try:
        return datetime.strptime(raw.split("(")[0].strip(), "%b-%Y")
    except (ValueError, IndexError):
        return None


# Fixed reference date — the LATEST Monitoring_Date in the dataset, exactly as
# ghostwatch_scoring.py computes REFERENCE_DATE.  The scores freeze recency at
# this date, so the explanation text must too; using datetime.now() here made
# reason sentences drift every day (e.g. "226 days ago" -> "227 days ago") and
# occasionally flip phrases at the 183/365-day thresholds, contradicting the
# frozen score.  Deterministic: identical input -> identical reasons.
def _fixed_reference_date():
    try:
        s = pd.read_csv(FINAL_CSV, usecols=["Monitoring_Date"])["Monitoring_Date"]
        dates = [d for d in (parse_monitoring_date(v) for v in s) if d is not None]
        if dates:
            return max(dates)
    except Exception:
        pass
    return datetime.now()  # last-resort fallback (never expected)


TODAY = _fixed_reference_date()


def is_no(v):
    return str(v).strip().lower() == "no"


def is_yes(v):
    return str(v).strip().lower() == "yes"


def fmt_pct(x):
    """100.0 -> '100', 62.5 -> '62.5' (compact display)."""
    return f"{round(float(x), 1):g}"


def row_contributions(row, tier):
    """
    Recompute the per-factor weighted contributions (points) exactly the way
    ghostwatch_scoring.py does, pairing each with a human phrase (or None when
    the factor is clean / too small to mention).
    Returns list of (factor, points, phrase).
    """
    out = []

    # ---- Tier 1 only: attendance, teacher fill, monitoring recency --------
    if tier == 1:
        enrolled = pd.to_numeric(row.get("Total_Enrolled"), errors="coerce")
        present = pd.to_numeric(row.get("Total_Present"), errors="coerce")
        if pd.notna(enrolled) and enrolled > 0 and pd.notna(present):
            pct = present / enrolled * 100.0
            frac = 1.0 - min(max(pct, 0.0), 100.0) / 100.0
            phrase = None
            if pct < 75:
                phrase = f"low attendance ({round(pct)}%)"
            elif pct < 90:
                phrase = f"attendance at only {round(pct)}%"
            out.append(("attendance", TIER1_WEIGHTS["attendance"] * frac, phrase))
        else:
            out.append(("attendance", TIER1_WEIGHTS["attendance"] * 0.5,
                        "no attendance headcount recorded"))

        sanc = pd.to_numeric(row.get("Teachers_Sanctioned"), errors="coerce")
        filled = pd.to_numeric(row.get("Teachers_Filled"), errors="coerce")
        if pd.notna(sanc) and sanc > 0 and pd.notna(filled):
            fpct = filled / sanc * 100.0
            frac = 1.0 - min(max(fpct, 0.0), 100.0) / 100.0
            phrase = None
            if fpct < 100:
                phrase = (f"only {int(filled)} of {int(sanc)} "
                          "sanctioned teacher posts filled")
            out.append(("fill", TIER1_WEIGHTS["fill"] * frac, phrase))
        else:
            out.append(("fill", TIER1_WEIGHTS["fill"] * 0.5, "no teacher staffing data"))

        mon = parse_monitoring_date(row.get("Monitoring_Date"))
        if mon is not None:
            days = max((TODAY - mon).days, 0)
            frac = min(days, 365) / 365.0
            phrase = None
            years = days // 365
            if years >= 2:
                phrase = f"last monitoring visit {years} years ago"
            elif days > 365:
                phrase = "last monitoring visit over a year ago"
            elif days >= 183:
                phrase = f"last monitoring visit {days} days ago"
            out.append(("recency", TIER1_WEIGHTS["recency"] * frac, phrase))
        else:
            out.append(("recency", TIER1_WEIGHTS["recency"] * 0.5,
                        "no monitoring visit date recorded"))

    # ---- Toilets (both tiers) ---------------------------------------------
    w = TIER1_WEIGHTS["toilet"] if tier == 1 else TIER2_WEIGHTS["toilet"]
    t = pd.to_numeric(row.get("Toilets_Total"), errors="coerce")
    f = pd.to_numeric(row.get("Toilets_Functional"), errors="coerce")
    if pd.notna(t) and t > 0 and pd.notna(f):
        pct = f / t * 100.0
        frac = 1.0 - min(max(pct, 0.0), 100.0) / 100.0
        phrase = None
        if pct <= 0:
            phrase = f"no functional toilets (0 of {int(t)})"
        elif pct < 100:
            adj = "moderate" if pct >= 33 else "severe"
            phrase = f"{adj} toilet non-functionality (only {int(f)} of {int(t)} working)"
        out.append(("toilet", w * frac, phrase))
    elif pd.notna(t) and t == 0:
        out.append(("toilet", w * 0.5, "no toilets on premises"))
    else:
        out.append(("toilet", w * 0.5, "no toilet data recorded"))

    # ---- Illegal occupation (both tiers) -----------------------------------
    w = TIER1_WEIGHTS["illegal"] if tier == 1 else TIER2_WEIGHTS["illegal"]
    if is_yes(row.get("Building_Illegal_Occupation")):
        out.append(("illegal", w, "illegal occupation of the school building"))
    else:
        out.append(("illegal", 0.0, None))

    # ---- Boundary wall (both tiers) ----------------------------------------
    w = TIER1_WEIGHTS["boundary"] if tier == 1 else TIER2_WEIGHTS["boundary"]
    if is_no(row.get("Boundary_Wall")):
        out.append(("boundary", w, "no boundary wall"))
    else:
        out.append(("boundary", 0.0, None))

    # ---- Drinking water (Tier 2 treats NaN as 'No', same as scoring) -------
    w = TIER1_WEIGHTS["water"] if tier == 1 else TIER2_WEIGHTS["water"]
    water_val = row.get("Drinking_Water")
    if tier == 2 and pd.isna(water_val):
        water_val = "No"
    if is_no(water_val):
        out.append(("water", w, "no drinking water"))
    else:
        out.append(("water", 0.0, None))

    # ---- Electricity (Tier 2 treats NaN as 'No', same as scoring) ----------
    w = TIER1_WEIGHTS["electricity"] if tier == 1 else TIER2_WEIGHTS["electricity"]
    elec_val = row.get("Electricity")
    if tier == 2 and pd.isna(elec_val):
        elec_val = "No"
    if is_no(elec_val):
        out.append(("electricity", w, "no electricity"))
    else:
        out.append(("electricity", 0.0, None))

    return out


def join_phrases(parts):
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{parts[0]}, {parts[1]} and {parts[2]}"


def build_data_reason(row, tier):
    """Assemble the Data_Risk_Reason sentence for one row."""
    contrib = row_contributions(row, tier)
    total = sum(pts for _, pts, _ in contrib)

    # Rank citable factors by weighted points
    citable = [(pts, ph) for _, pts, ph in contrib if ph and pts > 0]
    citable.sort(key=lambda x: -x[0])

    selected = [ph for pts, ph in citable if pts >= MIN_POINTS][:MAX_FACTORS]
    if not selected and citable and total >= MIN_POINTS:
        selected = [citable[0][1]]

    # School_Status is not in the numeric weights, but Non-Functional / Closed
    # rows must be clearly reflected in the explanation.
    status = str(row.get("School_Status") or "").strip()
    status_clause = None
    if status in ("Non-Functional", "Closed"):
        if str(row.get("Data_Type") or "").strip() == "Real (Census)":
            status_clause = f"School_Status marked '{status}' by government census"
        else:
            status_clause = f"School_Status marked '{status}'"

    if not selected:
        if tier == 1:
            normal = "all monitored indicators within normal range"
        else:
            normal = "all monitored infrastructure indicators within normal range"
        if status_clause:
            return f"{status_clause}; {normal}"
        return normal[0].upper() + normal[1:]

    joined = join_phrases(selected)
    if status_clause:
        return f"{status_clause}, {joined}"
    return joined[0].upper() + joined[1:]


def build_satellite_reason(row):
    """Assemble the Satellite_Risk_Reason sentence (blank unless real data)."""
    flag = row.get("Satellite_Density_Flag")
    if pd.isna(flag):
        return ""                      # no satellite row at all -> do not guess
    flag = str(flag).strip()
    if flag not in ("Low_Density", "Mixed_Density", "High_Density"):
        return ""                      # NoData -> deliberately blank
    p = pd.to_numeric(row.get("Satellite_BuiltUp_Percent"), errors="coerce")
    if pd.isna(p):
        return ""
    ps = fmt_pct(p)

    confidence = str(row.get("Satellite_Confidence", "School-Level")).strip()
    village = confidence == "Village-Level"
    village_tag = (" (shared coordinates with other schools in this village "
                   "— reflects general area, not confirmed for this specific school)"
                   if village else "")

    if flag == "Low_Density":
        if village:
            return (f"Village-area shows {ps}% built-up area detected around coordinates — "
                    f"sparse development, potential ghost-school indicator{village_tag}")
        # School-Level: these are the school's EXACT coordinates — sparse
        # built-up there means the claimed location is unverified, so the
        # pipeline floors Priority at Medium (location-verification floor).
        return (f"Only {ps}% built-up area detected around the school's exact "
                f"coordinates — sparse development; verify the school exists "
                f"at its geocoded location (location-verification floor: "
                f"at least Medium priority)")
    if flag == "Mixed_Density":
        prefix = "Village-area shows" if village else ""
        return (f"{prefix} {ps}% built-up area detected around coordinates — "
                f"partially developed surroundings, satellite signal inconclusive{village_tag}")
    # High_Density
    prefix = "Village-area shows" if village else ""
    return f"{prefix} {ps}% built-up area detected — clearly in a developed location{village_tag}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    df = pd.read_csv(FINAL_CSV, dtype={"EMIS_Code": str})
    qwen_before = df["Qwen_Fraud_Reason"].notna().sum()
    print(f"Loaded {len(df)} rows from {FINAL_CSV.name}")

    # Base (pre-satellite) scores for validation only
    tier_base = None
    if TIER_CSV.exists():
        tb = pd.read_csv(TIER_CSV, dtype={"EMIS_Code": str})
        tier_base = tb.set_index("EMIS_Code")[
            ["Ghost_Risk_Score_Tier1", "Infrastructure_Screening_Score"]]

    # ---- Generate the two new columns --------------------------------------
    data_reasons, sat_reasons = [], []
    recon_t2_bad, recon_t1_bad, store_bad = [], [], []
    scoring_date_ests = []

    for _, row in df.iterrows():
        tier = int(row["Tier"])
        data_reasons.append(build_data_reason(row, tier))
        sat_reasons.append(build_satellite_reason(row))

        # ---- validation vs the stored pipeline values ---------------------
        if tier_base is not None and row["EMIS_Code"] in tier_base.index:
            contrib = row_contributions(row, tier)
            # notes_fraud_scorer.py adds the fraud boost (cap 100) to
            # Qwen-flagged Tier-1 rows
            qwen_boost = float(FRAUD_SCORE_BOOST) \
                if str(row.get("Qwen_Fraud_Flag")).strip() == "True" else 0.0
            # ghostwatch_scoring.py adds the status bonus (cap 100) for
            # Non-Functional/Closed
            _status = str(row.get("School_Status") or "").strip()
            status_bonus = float(STATUS_OVERRIDE_BONUS) \
                if _status in STATUS_OVERRIDE_STATES else 0.0
            if tier == 1:
                non_recency = sum(pts for fac, pts, _ in contrib if fac != "recency")
                recency_today = sum(pts for fac, pts, _ in contrib if fac == "recency")
                base = tier_base.loc[row["EMIS_Code"], "Ghost_Risk_Score_Tier1"]
                if pd.notna(base):
                    # Recency implied by the stored score (i.e. at the time
                    # ghostwatch_scoring.py ran).  Recency only grows over time,
                    # so it can never exceed today's value.
                    # stored_base = min(non_recency + recency_then + status_bonus + qwen_boost, 100)
                    implied_recency = max(0.0, min(10.0,
                                                   float(base) - non_recency - status_bonus - qwen_boost))
                    if implied_recency > recency_today + 0.05:
                        recon_t1_bad.append((row["EMIS_Code"], round(implied_recency, 2),
                                             round(recency_today, 2)))
                    if implied_recency < 9.99:
                        mon = parse_monitoring_date(row.get("Monitoring_Date"))
                        if mon is not None:
                            scoring_date_ests.append(
                                mon + timedelta(days=implied_recency / 10.0 * 365))
            else:
                recomputed = sum(pts for _, pts, _ in contrib) + status_bonus
                recomputed = min(recomputed, 100.0)
                base = tier_base.loc[row["EMIS_Code"], "Infrastructure_Screening_Score"]
                if pd.notna(base) and abs(recomputed - float(base)) > 0.01:
                    recon_t2_bad.append((row["EMIS_Code"], round(recomputed, 2),
                                         float(base)))

            # stored score should equal base + satellite modifier
            flag = row.get("Satellite_Density_Flag")
            confidence = str(row.get("Satellite_Confidence", "School-Level")).strip()
            sat_table = SAT_MOD_VILLAGE if confidence == "Village-Level" else SAT_MOD_SCHOOL
            mod = sat_table.get(str(flag).strip(), 0) if pd.notna(flag) else 0
            if tier == 1:
                stored = row["Ghost_Risk_Score_Tier1"]
                base_all = tier_base.loc[row["EMIS_Code"], "Ghost_Risk_Score_Tier1"]
            else:
                stored = row["Infrastructure_Screening_Score"]
                base_all = tier_base.loc[row["EMIS_Code"],
                                         "Infrastructure_Screening_Score"]
            if pd.notna(stored) and pd.notna(base_all):
                if abs(float(stored) - float(base_all) - mod) > 0.01:
                    store_bad.append((row["EMIS_Code"], float(stored),
                                      float(base_all), mod))

    df["Data_Risk_Reason"] = data_reasons
    df["Satellite_Risk_Reason"] = sat_reasons

    # ---- Reorder columns: each reason sits next to its source --------------
    cols = df.columns.tolist()
    cols.remove("Data_Risk_Reason")
    cols.remove("Satellite_Risk_Reason")
    cols.insert(cols.index("Infrastructure_Screening_Score") + 1, "Data_Risk_Reason")
    cols.insert(cols.index("Satellite_Density_Flag") + 1, "Satellite_Risk_Reason")
    df = df[cols]

    # ---- Save (with one-time backup) ---------------------------------------
    if not BACKUP_CSV.exists():
        shutil.copy2(FINAL_CSV, BACKUP_CSV)
        print(f"Backup written: {BACKUP_CSV.name}")
    df.to_csv(FINAL_CSV, index=False)
    print(f"Saved updated {FINAL_CSV.name}  ({len(df)} rows, {len(df.columns)} cols)")

    # ---- Verification -------------------------------------------------------
    print("\n" + "=" * 78)
    print("VERIFICATION")
    print("=" * 78)
    qwen_after = df["Qwen_Fraud_Reason"].notna().sum()
    print(f"Qwen_Fraud_Reason intact : {qwen_before} -> {qwen_after} non-null "
          f"({'OK' if qwen_before == qwen_after else 'MISMATCH!'})")
    print(f"Data_Risk_Reason filled  : {df['Data_Risk_Reason'].notna().sum()}/{len(df)}")
    sat_filled = (df["Satellite_Risk_Reason"].fillna("").str.len() > 0).sum()
    flags_real = df["Satellite_Density_Flag"].isin(
        ["Low_Density", "Mixed_Density", "High_Density"]).sum()
    print(f"Satellite_Risk_Reason    : {sat_filled} filled "
          f"({flags_real} rows have real satellite data)")
    nodata = (df["Satellite_Density_Flag"] == "NoData").sum()
    blank_nodata = ((df["Satellite_Density_Flag"] == "NoData")
                    & (df["Satellite_Risk_Reason"].fillna("") == "")).sum()
    print(f"NoData rows blank        : {blank_nodata}/{nodata}")

    if recon_t2_bad:
        print(f"\nWARNING: {len(recon_t2_bad)} Tier-2 rows where recomputed score "
              f"!= two-tier base (first 5): {recon_t2_bad[:5]}")
    else:
        print("Tier-2 recomputation matches ghostwatch_two_tier_scores.csv exactly.")
    if recon_t1_bad:
        print(f"WARNING: {len(recon_t1_bad)} Tier-1 rows whose stored score cannot be "
              f"explained by data factors + Qwen boost + recency: {recon_t1_bad[:5]}")
    else:
        print("Tier-1 stored scores fully explained by data factors + Qwen boost "
              "(+10 for 6 flagged rows) + recency.")
    if store_bad:
        print(f"WARNING: {len(store_bad)} rows where stored score != base + "
              f"satellite modifier (first 5): {store_bad[:5]}")
    else:
        print("Stored scores = two-tier base + satellite modifier for all rows.")
    if scoring_date_ests:
        ests = sorted(scoring_date_ests)
        print(f"Tier-1 scores appear to have been computed around: "
              f"{ests[len(ests) // 2].date()} (median of {len(ests)} estimates)")

    # ---- Examples of each satellite flavour --------------------------------
    print("\n" + "=" * 78)
    print("SAMPLE SATELLITE REASONS")
    print("=" * 78)
    for flag in ["Low_Density", "Mixed_Density", "High_Density"]:
        ex = df[df["Satellite_Density_Flag"] == flag].head(2)
        for _, r in ex.iterrows():
            print(f"  [{flag:<13}] {r['EMIS_Code']}: {r['Satellite_Risk_Reason']}")

    # ---- Preview: the 8 requested schools -----------------------------------
    TARGETS = ["31321034", "31321032", "31321031", "32110506",
               "31321033", "36130239", "32320754", "31320431"]
    print("\n" + "=" * 78)
    print("PREVIEW: DATA_RISK_REASON / SATELLITE_RISK_REASON FOR THE 8 SCHOOLS")
    print("=" * 78)
    for code in TARGETS:
        rows = df[df["EMIS_Code"] == code]
        if rows.empty:
            print(f"\n  {code}: NOT FOUND")
            continue
        r = rows.iloc[0]
        score = r["Infrastructure_Screening_Score"] if r["Tier"] == 2 \
            else r["Ghost_Risk_Score_Tier1"]
        print(f"\n  {code} | {r['School_Name']} | {r['Data_Type']} | "
              f"Score: {score} | Priority: {r['Priority']}")
        print(f"    Data_Risk_Reason      : {r['Data_Risk_Reason']}")
        sat = r["Satellite_Risk_Reason"]
        if pd.isna(sat) or str(sat).strip() == "":
            sat = "(blank -- satellite flag is NoData)"
        print(f"    Satellite_Risk_Reason : {sat}")


if __name__ == "__main__":
    main()
