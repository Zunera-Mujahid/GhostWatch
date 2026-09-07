"""
build_final_reason_and_map.py
==============================

Post-processing step that:
  1. Generates the Final_GhostSchool_Reason column: a natural-language
     explanation (2-3 sentences) that presents the data/risk indicators and
     the satellite finding, explains how the two relate, and closes with a
     recommendation calibrated to the school's Priority.  When inspection
     notes were analysed, a separate "Monitoring Notes:" line follows on its
     own line.  When the two satellite epochs differ by at least
     _CHANGE_MENTION_PP percentage points, the satellite sentence also cites
     the built-up trend (e.g. "(down from 92% in the 2017 imagery)") -- a
     factual observation that never alters scores or Priority.
  2. Generates an interactive Folium map (ghostwatch_map.html) with color-coded
     priority pins and Final_GhostSchool_Reason popups.

The reason is generated dynamically from each school's ACTUAL findings (never
hardcoded): Data_Risk_Reason, Satellite_Density_Flag / BuiltUp_Percent /
Satellite_Confidence, Percent_Built_Earliest / Percent_Built_Recent, Priority,
Tier and Qwen_Fraud_Reason.  Risk scores, satellite scores, thresholds and
Priority classifications are read-only here -- this step only rewrites the
human-readable explanation and its formatting.

Run:  python build_final_reason_and_map.py
"""
import html
import os
import re
import sys
import pandas as pd
import numpy as np

from ghostwatch_config import SATELLITE_EPOCH_YEARS

# Force UTF-8 on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_CSV = os.path.join(SCRIPT_DIR, "ghostwatch_final_merged_v2.csv")
OUTPUT_CSV = INPUT_CSV  # overwrite in place
MAP_HTML = os.path.join(SCRIPT_DIR, "ghostwatch_map.html")


# ---------------------------------------------------------------------------
# 1. Load data
# ---------------------------------------------------------------------------
df = pd.read_csv(INPUT_CSV)
print(f"Loaded {len(df)} rows, {len(df.columns)} cols from "
      f"'{os.path.basename(INPUT_CSV)}'")


# ---------------------------------------------------------------------------
# 2. Build Final_GhostSchool_Reason  (natural-language, relationship-aware)
# ---------------------------------------------------------------------------
#
# The reason is no longer a plain concatenation of the per-source reasons:
# it explains the data finding, the satellite finding and their RELATIONSHIP
# in plain English, with wording chosen for the actual combination:
#
#   data side      -> Data_Risk_Reason (built by add_explanation_columns.py),
#                     classified as concerning unless it reports that all
#                     monitored indicators are within normal range; the Tier
#                     (monitoring visit vs census records) sets the lead-in.
#   satellite side -> Satellite_Density_Flag + Satellite_BuiltUp_Percent,
#                     qualified by Satellite_Confidence (the school's exact
#                     coordinates vs the shared village area).  The built-up
#                     trend between the two satellite epochs
#                     (Percent_Built_Earliest vs Percent_Built_Recent) is
#                     cited when the change is >= _CHANGE_MENTION_PP points.
#   relationship   -> corroborates / contradicts / inconclusive / unavailable.
#   recommendation -> calibrated to the school's Priority (read-only).
#   notes          -> Qwen_Fraud_Reason rendered on its own "Monitoring Notes:"
#                     line so it never runs into the explanation body.
#
# The body is capped at 2-3 sentences; no causal claims are made between the
# data and satellite findings.

_SAT_KINDS = {"Low_Density": "sparse", "Mixed_Density": "mixed",
              "High_Density": "developed"}

_SAT_WHERE = {
    "exact": "around the school's exact location",
    "village": "in the surrounding village area",
    "tehsil": "around the tehsil centre (an approximate location)",
}

# Years of the two LULC epochs the satellite sampling compared
_EPOCH_EARLIEST, _EPOCH_RECENT = SATELLITE_EPOCH_YEARS

# Presentation-only threshold: cite the built-up trend between the two
# satellite epochs only when the change is at least this many percentage
# points (smaller grid-sampled swings are usually noise).  Purely wording --
# never affects scores, flags or Priority.
_CHANGE_MENTION_PP = 10


def _fmt_pct(x):
    """100.0 -> '100', 62.5 -> '62.5' (compact display)."""
    try:
        return f"{round(float(x), 1):g}"
    except (TypeError, ValueError):
        return None


def _cap(text):
    """Capitalise the first character of a sentence lead-in."""
    return text[0].upper() + text[1:] if text else text


def _data_finding(row):
    """
    Classify the data-analysis side of a school.

    Returns (is_concerning, phrase); the phrase is the Data_Risk_Reason with
    its lead word lower-cased where safe, ready to embed after '... shows'.
    add_explanation_columns.py guarantees that a reason beginning with
    'all monitored ...' cites no risk factor (data side clean); anything
    else -- including a leading 'School_Status ...' clause -- indicates at
    least one concerning indicator.
    """
    raw = row.get("Data_Risk_Reason")
    if pd.isna(raw) or not str(raw).strip():
        return False, None
    txt = " ".join(str(raw).split())      # collapse stray whitespace
    is_concerning = not txt.lower().startswith("all monitored")
    phrase = txt if txt.startswith("School_Status") else txt[0].lower() + txt[1:]
    return is_concerning, phrase


def _satellite_finding(row):
    """
    Classify the satellite side.

    Returns (kind, scope, pct):
      kind  'sparse' | 'developed' | 'mixed' | 'unavailable'
      scope 'exact' (school's own coordinates) | 'village' (coordinates shared
            with other schools) | 'tehsil' (centroid fallback) | None
      pct   formatted built-up percentage, or None when unavailable
    """
    flag = row.get("Satellite_Density_Flag")
    flag = str(flag).strip() if pd.notna(flag) else ""
    conf = str(row.get("Satellite_Confidence") or "").strip()
    scope = {"School-Level": "exact", "Village-Level": "village",
             "Tehsil-Level": "tehsil"}.get(conf)
    pct = _fmt_pct(row.get("Satellite_BuiltUp_Percent"))
    if flag not in _SAT_KINDS or pct is None:
        return "unavailable", scope, None
    return _SAT_KINDS[flag], scope, pct


def _builtup_change(row):
    """
    Detect a notable built-up change between the two satellite epochs.

    Returns (old_pct, direction) with direction 'up'/'down' when both epoch
    readings exist and differ by at least _CHANGE_MENTION_PP percentage
    points, else None.  Presentation-only: never affects scores/Priority.
    """
    e = pd.to_numeric(row.get("Percent_Built_Earliest"), errors="coerce")
    r = pd.to_numeric(row.get("Percent_Built_Recent"), errors="coerce")
    if pd.isna(e) or pd.isna(r) or abs(r - e) < _CHANGE_MENTION_PP:
        return None
    return _fmt_pct(e), ("up" if r > e else "down")


def _trend_note(change):
    """' (up from 0% in the 2017 imagery)' -- or '' when no notable change."""
    if not change:
        return ""
    old, direction = change
    return f" ({direction} from {old}% in the {_EPOCH_EARLIEST} imagery)"


def _sat_clause(kind, scope, pct, change=None):
    """Natural-language fragment describing the satellite reading itself,
    citing the built-up trend between the epochs when it is notable."""
    where = _SAT_WHERE.get(scope, "around the recorded location")
    trend = _trend_note(change)
    if kind == "developed":
        return f"{pct}% built-up {where}{trend}, suggesting a clearly developed location"
    if kind == "sparse":
        return f"only {pct}% built-up {where}{trend}, indicating a sparsely developed area"
    return f"{pct}% built-up {where}{trend}"


def _notes_line(row):
    """
    Build the separate 'Monitoring Notes:' line (None when no notes exist).

    Fraud-flagged rows quote the matched category and trigger phrase from
    notes_fraud_scorer.py's '<category>: matched "<trigger>"' format; rows
    whose notes were analysed but clean get a one-line reassurance; rows
    with no notes at all get no line.
    """
    flag = str(row.get("Qwen_Fraud_Flag") or "").strip()
    raw = row.get("Qwen_Fraud_Reason")
    txt = str(raw).strip() if pd.notna(raw) else ""
    if flag == "True":
        if not txt:
            return "Monitoring Notes: Flagged for fraud in the inspection notes."
        m = re.match(r'^(.*?):\s*matched\s+"(.*)"\s*$', txt)
        if m:
            category, trigger = m.group(1).strip(), m.group(2).strip()
            return (f"Monitoring Notes: Flagged for fraud — {category} "
                    f'(inspector notes mention "{trigger}").')
        return f"Monitoring Notes: Flagged for fraud — {txt}."
    if txt:
        return ("Monitoring Notes: No fraud indicators detected in the "
                "inspection notes.")
    return None


def _final_reason_body(row):
    """Build the 2-3 sentence explanation body for one school."""
    concerning, data = _data_finding(row)
    if data is None:
        return None

    kind, scope, pct = _satellite_finding(row)
    change = _builtup_change(row)
    where = _SAT_WHERE.get(scope, "around the recorded location")
    prio = str(row.get("Priority") or "").strip()
    fraud = str(row.get("Qwen_Fraud_Flag") or "").strip() == "True"

    # Lead-in reflects data confidence: Tier 1 rows carry full monitoring-
    # visit data, Tier 2 rows only census infrastructure records.
    tier_val = row.get("Tier")
    tier1 = pd.notna(tier_val) and str(tier_val).strip() in ("1", "1.0")
    if tier1:
        show = f"the data analysis shows {data}"
    else:
        show = f"the census-based data analysis shows {data}"

    sat = _sat_clause(kind, scope, pct, change)
    s = []

    if concerning and kind == "developed":
        # Concerning data vs a built-up, developed location: findings disagree.
        s.append(f"Although satellite imagery shows {sat}, {show}.")
        s.append("The two findings therefore point in different directions: "
                 "a developed setting makes a physically absent school less "
                 "likely, yet the recorded indicators remain weak.")
        if fraud:
            s.append("Together with the fraud flag in the monitoring notes, "
                     "this discrepancy is a significant red flag and "
                     "warrants prompt investigation.")
        elif prio == "High":
            s.append("This discrepancy is a red flag and warrants prompt "
                     "investigation.")
        elif prio == "Medium":
            s.append("This discrepancy is worth a follow-up review to "
                     "determine whether the school is operating as reported.")
        else:
            s.append("On its own this discrepancy is not a strong "
                     "ghost-school signal, though it merits routine "
                     "monitoring.")

    elif concerning and kind == "sparse":
        # Concerning data + sparse surroundings: findings corroborate.
        s.append(f"Both findings point in the same direction: {show}, and "
                 f"satellite imagery shows {sat}.")
        if scope == "exact":
            s.append("Sparse built-up at the school's exact coordinates "
                     "leaves its presence at the recorded location "
                     "unverified, so the two findings reinforce each other "
                     "and an on-site verification is warranted.")
        else:
            s.append("Because the satellite reading reflects the general "
                     "village area rather than the school's exact site, it "
                     "is supporting context rather than confirmation, but "
                     "it is consistent with the weak data indicators.")
            if prio == "High":
                s.append("This warrants prompt investigation.")
            elif prio == "Medium":
                s.append("A follow-up review is recommended.")

    elif concerning and kind == "mixed":
        # Concerning data; the satellite signal is inconclusive.
        s.append(f"{_cap(show)}.")
        s.append(f"Satellite imagery is inconclusive here: it shows {pct}% "
                 f"built-up {where}{_trend_note(change)} — partially developed "
                 "surroundings that neither confirm nor rule out a functioning "
                 "school at this location.")
        if fraud:
            s.append("Together with the fraud flag in the monitoring notes, "
                     "this warrants prompt investigation.")
        elif prio == "High":
            s.append("The data indicators alone therefore warrant prompt "
                     "investigation.")
        elif prio == "Medium":
            s.append("The data indicators alone warrant a follow-up review.")

    elif concerning:
        # Concerning data; no satellite reading available.
        s.append(f"{_cap(show)}.")
        if scope == "tehsil":
            s.append("No usable satellite reading is available — the school "
                     "could only be geocoded to its tehsil centre, and "
                     "imagery there would not represent its actual location.")
        else:
            s.append("No satellite reading is available for this school's "
                     "location.")
        if fraud:
            s.append("Together with the fraud flag in the monitoring notes, "
                     "the data indicators alone warrant prompt "
                     "investigation.")
        elif prio == "High":
            s.append("The data indicators alone warrant prompt "
                     "investigation, ideally combined with location "
                     "verification during the visit.")
        elif prio == "Medium":
            s.append("The data indicators alone warrant a follow-up review.")

    elif kind == "developed":
        # Clean data + developed surroundings: consistent, no signal.
        s.append(f"{_cap(show)}, and satellite imagery shows {sat}.")
        s.append("The two findings agree and are consistent with a "
                 "functioning school; no ghost-school signal is present.")
        if fraud:
            s.append("However, the fraud flag in the monitoring notes is a "
                     "concern that warrants investigation.")

    elif kind == "sparse":
        # Clean data vs sparse surroundings: the opposite mismatch.
        s.append(f"{_cap(show)}, but satellite imagery shows {sat}.")
        if scope == "exact":
            s.append("The findings disagree: a functioning school would "
                     "normally sit within a settled area, so the school's "
                     "presence at its recorded coordinates remains "
                     "unverified — imagery alone cannot distinguish a "
                     "geocoding error from an absent school.")
            s.append("A verification visit is recommended to confirm the "
                     "school exists at its recorded location.")
        else:
            s.append("Because the reading reflects the general village area "
                     "rather than the school's exact site, it is weak "
                     "evidence on its own; with no data indicators of "
                     "concern either, no strong ghost-school signal is "
                     "present.")
        if fraud:
            s.append("However, the fraud flag in the monitoring notes is a "
                     "concern that warrants investigation.")

    elif kind == "mixed":
        # Clean data; the satellite signal is inconclusive.
        s.append(f"{_cap(show)}, while satellite imagery shows {pct}% built-up "
                 f"{where}{_trend_note(change)} — partially developed "
                 "surroundings, an inconclusive signal on its own.")
        s.append("Neither source provides a strong ghost-school signal.")
        if fraud:
            s.append("However, the fraud flag in the monitoring notes is a "
                     "concern that warrants investigation.")

    else:
        # Clean data; no satellite reading available.
        s.append(f"{_cap(show)}.")
        if scope == "tehsil":
            s.append("No usable satellite reading is available — the school "
                     "could only be geocoded to its tehsil centre — so this "
                     "assessment rests on the data indicators alone.")
        else:
            s.append("No satellite reading is available for this school's "
                     "location, so this assessment rests on the data "
                     "indicators alone.")
        if fraud:
            s.append("However, the fraud flag in the monitoring notes is a "
                     "concern that warrants investigation.")

    return s or None


def build_final_reason(row):
    """
    Assemble the full Final_GhostSchool_Reason for one row: a 2-3 sentence
    explanation body followed, on its own line, by a 'Monitoring Notes:'
    line whenever inspection notes were analysed.
    """
    body = _final_reason_body(row)
    if not body:
        return np.nan
    parts = [" ".join(body)]
    notes = _notes_line(row)
    if notes:
        parts.append(notes)
    return "\n".join(parts)


df["Final_GhostSchool_Reason"] = df.apply(build_final_reason, axis=1)

# Insert column right after Satellite_Risk_Reason for logical grouping
cols = list(df.columns)
if "Final_GhostSchool_Reason" in cols:
    cols.remove("Final_GhostSchool_Reason")
# Place it after Satellite_Risk_Reason (or after Qwen_Fraud_Reason if satellite is missing)
try:
    insert_idx = cols.index("Satellite_Risk_Reason") + 1
except ValueError:
    try:
        insert_idx = cols.index("Qwen_Fraud_Reason") + 1
    except ValueError:
        insert_idx = len(cols)
cols.insert(insert_idx, "Final_GhostSchool_Reason")
df = df[cols]

# Stats
filled = df["Final_GhostSchool_Reason"].notna().sum()
reasons = df["Final_GhostSchool_Reason"].dropna().astype(str)
n_notes = reasons.str.contains("Monitoring Notes:").sum()
print(f"Final_GhostSchool_Reason: {filled}/{len(df)} filled "
      f"({n_notes} with a separate Monitoring Notes line)")

# Coverage: every school must land in exactly one explanation branch
data_side = df.apply(lambda r: "concerning" if _data_finding(r)[0] else "normal",
                     axis=1)
sat_side = df.apply(lambda r: _satellite_finding(r)[0], axis=1)
print("\nExplanation coverage (data finding x satellite finding):")
print(pd.crosstab(data_side, sat_side).to_string())

# Built-up trend mentions (presentation-only)
n_trend = int(df.apply(lambda r: _builtup_change(r) is not None, axis=1).sum())
print(f"\nBuilt-up trend cited: {n_trend} rows "
      f"(|{_EPOCH_RECENT} - {_EPOCH_EARLIEST}| >= {_CHANGE_MENTION_PP} pp)")

# Show sample combined reasons
print("\n" + "=" * 70)
print("SAMPLE FINAL REASONS")
print("=" * 70)
samples = df[df["Final_GhostSchool_Reason"].notna()].head(8)
for _, r in samples.iterrows():
    print(f"\n  {int(r['EMIS_Code'])} | {r['School_Name']} | Priority: {r['Priority']}")
    for line in str(r["Final_GhostSchool_Reason"]).split("\n"):
        print(f"    {line}")


# ---------------------------------------------------------------------------
# 3. Save updated CSV
# ---------------------------------------------------------------------------
df.to_csv(OUTPUT_CSV, index=False)
print(f"\nSaved updated CSV: {os.path.basename(OUTPUT_CSV)}  "
      f"({len(df)} rows, {len(df.columns)} cols)")


# ---------------------------------------------------------------------------
# 4. Build Folium map
# ---------------------------------------------------------------------------
import folium

# Filter to rows with coordinates (coerce to numeric for safety)
geo = df.copy()
geo["Latitude"] = pd.to_numeric(geo["Latitude"], errors="coerce")
geo["Longitude"] = pd.to_numeric(geo["Longitude"], errors="coerce")
geo = geo[geo["Latitude"].notna() & geo["Longitude"].notna()]
print(f"\nSchools with coordinates: {len(geo)}/{len(df)}")

# Centre map on the median coordinate (Punjab region)
center_lat = geo["Latitude"].median()
center_lon = geo["Longitude"].median()

m = folium.Map(location=[center_lat, center_lon], zoom_start=7,
               tiles="OpenStreetMap")

PRIORITY_COLORS = {"High": "red", "Medium": "orange", "Low": "green"}

# Add a legend
legend_html = """
<div style="position:fixed; bottom:30px; left:30px; z-index:1000;
            background:white; padding:12px 16px; border-radius:8px;
            border:2px solid grey; font-size:14px; line-height:1.8;">
  <b>GhostWatch Risk Priority</b><br>
  <span style="color:red;">&#9679;</span> High &mdash; Immediate audit<br>
  <span style="color:orange;">&#9679;</span> Medium &mdash; Schedule review<br>
  <span style="color:green;">&#9679;</span> Low &mdash; No strong signal
</div>
"""
m.get_root().html.add_child(folium.Element(legend_html))

# Add markers
for _, row in geo.iterrows():
    color = PRIORITY_COLORS.get(str(row["Priority"]).strip(), "gray")
    school_name = str(row.get("School_Name", "Unknown"))
    emis = str(int(row["EMIS_Code"])) if pd.notna(row["EMIS_Code"]) else "?"
    priority = str(row.get("Priority", "?"))
    score_col = "Ghost_Risk_Score_Tier1" if pd.notna(row.get("Ghost_Risk_Score_Tier1")) else "Infrastructure_Screening_Score"
    score = row.get(score_col, "?")
    # Compact display: 82.5 stays 82.5, 40.0 shows as 40 (matches dashboard)
    try:
        score = f"{float(score):g}"
    except (TypeError, ValueError):
        pass
    reason = str(row.get("Final_GhostSchool_Reason", ""))
    # Escape the text and turn newlines into line breaks so the separate
    # "Monitoring Notes:" line stays on its own line inside the popup
    reason_html = html.escape(reason).replace("\n", "<br>")

    # Build popup HTML (keeps it clean with line breaks)
    popup_html = (
        f"<div style='min-width:280px; max-width:380px;'>"
        f"<b>{school_name}</b><br>"
        f"<b>EMIS:</b> {emis} &nbsp;|&nbsp; "
        f"<b>Priority:</b> <span style='color:{color};'>{priority}</span> &nbsp;|&nbsp; "
        f"<b>Score:</b> {score}<br>"
        f"<hr style='margin:4px 0;'>"
        f"<i>{reason_html}</i>"
        f"</div>"
    )

    folium.CircleMarker(
        location=[row["Latitude"], row["Longitude"]],
        radius=7,
        color=color,
        fill=True,
        fill_color=color,
        fill_opacity=0.8,
        weight=2,
        popup=folium.Popup(popup_html, max_width=400),
        tooltip=f"{school_name} ({priority})",
    ).add_to(m)

m.save(MAP_HTML)
print(f"Map saved: {MAP_HTML}")

# Priority breakdown on the map
prio_counts = geo["Priority"].value_counts()
print(f"\nMap marker breakdown:")
for p in ["High", "Medium", "Low"]:
    print(f"  {p:>6}: {prio_counts.get(p, 0)} schools")
print(f"\nDone.")
