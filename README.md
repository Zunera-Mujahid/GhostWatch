# GhostWatch

AI-powered ghost school detector for Punjab, Pakistan — combines government monitoring
data, satellite imagery, and transparent rule-based fraud screening to flag non-functional
or fraudulent schools. Built for Alkhidmat x Alibaba Cloud AI Hackathon Pakistan 2026.

## The Problem

Punjab's school system keeps detailed records for thousands of schools, but a school
can exist on paper while no real education happens there. A **ghost school** has
enrolled students, sanctioned teachers, and a budget in the records — yet little or no
activity on the ground. Auditing every school manually is impossible, so auditors need
a prioritized shortlist with evidence they can verify.

GhostWatch scores 300 schools using three independent evidence streams:

1. **Government monitoring data** — PMIU visit records (physical attendance headcounts,
   teacher presence, facilities) and census infrastructure data
2. **Satellite imagery** — Sentinel-2 land-cover built-up analysis, plus ArcGIS World
   Imagery HD reference photos shown in the dashboard
3. **Fraud-note screening** — a transparent, rule-based pattern matcher (regex
   categories A–E in `notes_fraud_scorer.py`) scans inspectors' Notes for explicit
   fraud signals. The `Qwen_` column names are historical; no LLM call is involved
   in the current pipeline.

**Dataset composition (300 rows):** 42 real PMIU-visit schools, 251 real census
schools, and **7 synthetic test cases** (EMIS 9999xxxx, named "FAKE-1"…"FAKE-7").
The synthetic rows are deliberate validation controls — ghost-school profiles with
known expected outcomes (all scored as expected: 5 High, 2 Low) — they verify the
model catches what it should before it is trusted on real schools. They are clearly
labelled in the dashboard (Data Type = Synthetic) and excluded from source links.

## Pipeline (brief)

Each stage consumes the previous stage's output:

| Stage | Script | What it does |
|---|---|---|
| 1 | `ghostwatch_scoring.py` | Two-tier base scores from `ghost_school_RAW_dataset_VERIFIED (1).csv`. Tier 1 (PMIU visits + synthetic test cases): attendance-weighted 100-point model. Tier 2 (census rows): infrastructure-only screening. → `ghostwatch_two_tier_scores.csv` |
| 2 | `notes_fraud_scorer.py` | Rule-based fraud screening of inspector Notes (regex categories A–E; the `Qwen_` column names are historical) → `Qwen_Fraud_Flag` / `Qwen_Fraud_Reason` (+10 boost for flagged Tier 1 schools) |
| 3 | Geocoding: `geoapify_fallback_geocode.py`, `integrate_manual_geocoding.py`, `geocode_remaining_46.py` | Latitude/Longitude per school; unmatched schools fall back to tehsil centroids (`Satellite_Confidence` = Tehsil-Level) |
| 4 | Satellite sampling (STAC / Impact Observatory LULC grid) | Built-up % around each school → `satellite_results_v2.csv` (`Satellite_Density_Flag`) |
| 5 | `merge_satellite_scores_v2.py` | Joins satellite data onto tier scores; applies confidence-aware modifiers: School-Level +15/+7, Village-Level +5/+2, **Tehsil-Level always +0** (a tehsil centroid is not the school's location — display context only) → `ghostwatch_final_merged_v2.csv` |
| 6 | `add_explanation_columns.py` | Human-readable `Data_Risk_Reason` and `Satellite_Risk_Reason` |
| 7 | `build_final_reason_and_map.py` | Relationship-aware `Final_GhostSchool_Reason` (2–3 sentences explaining the data finding, the satellite finding and how they relate, plus a separate `Monitoring Notes:` line when inspection notes were analysed; when the two satellite epochs — 2017 vs 2023 built-up — differ by ≥10 percentage points, the reason also cites the trend, e.g. "down from 92% in the 2017 imagery") + standalone `ghostwatch_map.html` |
| 8 | `fetch_hd_satellite_images.py` | ArcGIS World Imagery HD reference photos (~0.3–0.6 m/pixel, with placeholder detection and zoom fallback) into `satellite_images/` |

`ghostwatch_final_merged_v2.csv` is the single source of truth served by the backend.

**Scoring constants** — tier weights, priority thresholds, satellite modifiers,
escalation rules and satellite epoch years all live in `ghostwatch_config.py`, the
single source of truth imported by every pipeline stage (no more copy-pasted
thresholds drifting apart between scripts). `tests/test_config_guard.py` freezes
their values, so an accidental edit fails loudly; follow the CHANGE PROTOCOL in
its docstring when changing one deliberately.

## Running

**One-click (Windows):** double-click `start.bat` — it installs dependencies if
needed, starts the backend, waits for it, and opens the dashboard.

**Backend** (FastAPI — serves school data, stats, and satellite images on port 8000):

```
cd backend
pip install -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

**Frontend**: open `frontend/index.html` in a browser (it calls the API at
`http://localhost:8000`). The standalone map is `ghostwatch_map.html`.
Interactive API docs: `http://localhost:8000/docs`.

## Key Columns (Glossary)

| Column | Meaning |
|---|---|
| `Priority` | Audit priority from the final score: **High** (≥ 50 — audit now), **Medium** (≥ 20 — schedule review), **Low** (< 20). Escalation rules: illegal occupation bumps one level; fraud-flagged notes escalate Medium → High; Non-Functional/Closed status forces at least High; **location-verification floor** — a School-Level school whose exact coordinates show Low_Density (almost no built-up) is floored at Medium, because its claimed location fails imagery verification (ghost school or bad geocode — either way, visit it). |
| `Ghost_Risk_Score_Tier1` | 0–100 risk score for Tier 1 schools (real PMIU visits + synthetic test cases); highest weight is the inverted attendance rate (30%). Tier 2 census schools use `Infrastructure_Screening_Score` (infrastructure-only screening) instead. |
| `Satellite_Confidence` | How precisely the coordinates locate *this* school: `School-Level` (unique coordinates, full satellite points), `Village-Level` (shared village coordinates, reduced points), `Tehsil-Level` (tehsil centroid — contributes **zero** points; the image is visual context only). |
| `Qwen_Fraud_Flag` | `True` when the rule-based note screener found explicit fraud signals in the inspector's Notes (6 of 300 schools). Adds +10 to Tier 1 score and escalates Medium → High. (The `Qwen_` prefix is historical — the screening is deterministic regex matching, not an LLM.) |
| `Final_GhostSchool_Reason` | Plain-language explanation (2–3 sentences) of the school's data/risk indicators, the satellite finding and how the two relate, with a recommendation calibrated to Priority; a separate `Monitoring Notes:` line follows when inspection notes were analysed; a notable built-up change between the 2017 and 2023 satellite epochs (≥10 pp) is cited in the satellite sentence. The dashboard verdict badge is driven by **Priority**: High → **YES** (ghost school — audit recommended), Medium → **POSSIBLE** (needs review), Low → **NO**. (Tier reflects data confidence, not the verdict.) |

## Dashboard Filters & Sorting

The filter bar supports District, Priority, Status, Fraud Flag (**Flagged First** pins fraud-flagged schools to the top), and **Satellite Risk** — **High First** pins the highest satellite-risk schools (almost no buildings around them) to the top; High/Medium/Low Only show just that satellite-risk band; Not Verified shows the 53 schools without a density estimate. All table columns are sortable (click the header arrows); Satellite Risk sorts by severity, not alphabetically. The default view lists the highest risk scores first.

## Source Data (Transparency)

Every school detail panel ends with a source-data action bar:

* **View raw data used for this score** (all schools) — opens a modal listing every column and value of that school's row in `ghostwatch_final_merged_v2.csv`, exactly as stored in the file (`GET /schools/{emis}/raw`), i.e. the underlying data behind the risk score, satellite flag and reason.
* **View original government record ↗** (PMIU-visit schools) — the school's own visit page on openpunjab.pesrp.edu.pk (deep link from `Source_URL`); the PESRP portal already highlights that exact school.
* **View this school's row in the census file** (census schools) — the Open Data Pakistan dataset page has no per-school view (full-file download only), so GhostWatch bundles the **real government census XLSX** (`census_source/`, stored unmodified) and opens it in a local viewer: the full 52,470-school × 108-column file as a searchable table, scrolled to and highlighting the clicked school's own row, matched by EMIS code (`GET /census/locate/{emis}`, `/census/rows`, `/census/search`).
* **Download full original government dataset ↗** (census schools) — the original Open Data Pakistan dataset page, kept visible for transparency that this is the real source.
* Synthetic test rows show a plain note instead of an external link (their `Source_URL` is explanatory text, not a URL).

## Project Layout

```
backend/                              FastAPI app (app.py) — API + image serving
census_source/                        Original government census XLSX (unmodified) + fast-loading CSV + provenance.json
prepare_census_source.py              One-time prep: census XLSX → CSV + provenance (run if census_source/ is missing)
frontend/                             Dashboard (single-page HTML/JS)
tests/                                QA audit suite (cross-surface checks, edge cases, perf, invariants, config guard, census viewer)
ghostwatch_config.py                  Single source of truth for all scoring constants
satellite_images/                     HD aerial reference photos (one PNG per school)
satellite_images_sentinel2_backup/    Original Sentinel-2 RGB images (backup)
ghostwatch_final_merged_v2.csv        Final merged dataset (source of truth)
backups/                              Pre-change snapshots of pipeline outputs
osm_overpass_tehsil_candidates.*      OSM/Overpass tehsil-geocoding research (kept as documentation)
```
