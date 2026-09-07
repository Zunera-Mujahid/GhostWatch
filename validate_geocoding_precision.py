"""
validate_geocoding_precision.py
================================
Reverse-geocode all schools that have coordinates to check whether
Nominatim considers the location precise (village/building) or coarse
(city/district centroid).

Strategy:
  - For each school with coordinates: reverse-geocode via Nominatim
  - If COARSE address type (city, state, county, administrative): REJECT
  - If PRECISE type or reverse fails: KEEP coordinates
  - Synthetic schools: always skip

Run:  python validate_geocoding_precision.py
"""

import sys
import time
import pandas as pd
from pathlib import Path

from geopy.geocoders import Nominatim

ROOT = Path(__file__).resolve().parent

# For REVERSE geocoding: reject only administrative/boundary-level types.
# Road types (primary, trunk, tertiary), POIs (station, attraction), and
# buildings are all SPECIFIC locations — not coarse centroids.
COARSE_REJECT_TYPES = {
    "administrative", "boundary", "political",
    "city", "town", "state", "county", "district", "region",
    "country", "continent", "municipality", "province",
}

# -- Load data ---------------------------------------------------------------
old_geo = pd.read_csv(ROOT / "geocoded_schools.csv", dtype={"EMIS_Code": str})
print(f"Loaded {len(old_geo)} schools from geocoded_schools.csv")
print(f"  With coordinates:    {old_geo['Latitude'].notna().sum()}")
print(f"  Without coordinates: {old_geo['Latitude'].isna().sum()}")

# -- Reverse geocode for precision validation --------------------------------
geolocator = Nominatim(user_agent="ghostwatch_precision_check")

new_lats = []
new_lons = []
new_sources = []
report_lines = []

n_kept = 0
n_rejected = 0
n_kept_fail = 0
n_synthetic = 0
n_no_coords = 0

coarse_types = {}
precise_types = {}

print(f"\nReverse-geocoding {old_geo['Latitude'].notna().sum()} schools...\n")

for idx, row in old_geo.iterrows():
    emis = row["EMIS_Code"]
    name = row.get("School_Name", "?")
    lat, lon = row["Latitude"], row["Longitude"]

    # Synthetic schools: skip
    if "Synthetic" in str(name):
        new_lats.append(None)
        new_lons.append(None)
        new_sources.append("Synthetic (no real location)")
        n_synthetic += 1
        continue

    # No coordinates: keep as-is
    if pd.isna(lat) or pd.isna(lon):
        new_lats.append(None)
        new_lons.append(None)
        new_sources.append("Not Geocoded (no coordinates available)")
        n_no_coords += 1
        continue

    # Reverse geocode
    try:
        location = geolocator.reverse(
            f"{lat}, {lon}", timeout=10,
            exactly_one=True, addressdetails=True,
        )
        raw = location.raw if location else {}
        addr_type = (raw.get("type") or "").lower().strip()
        addr_class = (raw.get("class") or "").lower().strip()

        # For reverse geocoding: REJECT only if type is coarse/admin-level.
        # Road types, POIs, buildings = specific locations (KEEP).
        is_coarse = (addr_type in COARSE_REJECT_TYPES or
                     addr_class in COARSE_REJECT_TYPES)
        is_precise = not is_coarse

        if is_precise:
            new_lats.append(lat)
            new_lons.append(lon)
            new_sources.append(f"Nominatim-reverse ({addr_type})")
            n_kept += 1
            precise_types[addr_type] = precise_types.get(addr_type, 0) + 1
            report_lines.append(
                f"KEEP   | {emis} | {str(name)[:40]:40s} | "
                f"type={addr_type:20s}")
        else:
            new_lats.append(None)
            new_lons.append(None)
            new_sources.append(
                f"Failed - too coarse ({addr_type or 'unknown'})")
            n_rejected += 1
            coarse_types[addr_type] = coarse_types.get(addr_type, 0) + 1
            report_lines.append(
                f"REJECT | {emis} | {str(name)[:40]:40s} | "
                f"type={addr_type:20s}")
            print(f"  [{idx+1}] REJECTED {emis} {name[:40]}: "
                  f"type={addr_type}")

    except Exception as e:
        new_lats.append(lat)
        new_lons.append(lon)
        new_sources.append(
            f"Nominatim-reverse (failed: {type(e).__name__})")
        n_kept_fail += 1
        report_lines.append(
            f"KEEP*  | {emis} | {str(name)[:40]:40s} | "
            f"reverse failed: {e}")

    time.sleep(1.1)

    if (idx + 1) % 25 == 0:
        print(f"  [{idx+1}/{len(old_geo)}] kept={n_kept} "
              f"rejected={n_rejected} failed={n_kept_fail}")

# -- Save outputs -------------------------------------------------------------
old_geo["Latitude"] = new_lats
old_geo["Longitude"] = new_lons
old_geo["Geocode_Source"] = new_sources

geo_path = ROOT / "geocoded_schools.csv"
old_geo.to_csv(geo_path, index=False)
print(f"\nSaved: {geo_path}  ({len(old_geo)} rows)")

sat_out = old_geo[["EMIS_Code", "Latitude", "Longitude"]].copy()
sat_path = ROOT / "satellite_results.csv"
sat_out.to_csv(sat_path, index=False)
print(f"Saved: {sat_path}  ({len(sat_out)} rows)")

report_path = ROOT / "_precision_report.txt"
with open(report_path, "w", encoding="utf-8") as f:
    f.write("PRECISION VALIDATION REPORT\n")
    f.write("=" * 80 + "\n\n")
    for line in report_lines:
        f.write(line + "\n")
print(f"Saved: {report_path}")

# -- Summary ------------------------------------------------------------------
print(f"\n{'='*80}")
print("VALIDATION SUMMARY")
print(f"{'='*80}")
print(f"  Total schools          : {len(old_geo)}")
print(f"  Synthetic (skipped)    : {n_synthetic}")
print(f"  No coords (unchanged)  : {n_no_coords}")
print(f"  KEPT (precise match)   : {n_kept}")
print(f"  KEPT (reverse failed)  : {n_kept_fail}")
print(f"  REJECTED (too coarse)  : {n_rejected}")

has_coords = sum(1 for x in new_lats
                 if x is not None and not (isinstance(x, float) and pd.isna(x)))
print(f"\n  Schools with coords now    : {has_coords}")
print(f"  Schools without coords now : {len(old_geo) - has_coords}")

if coarse_types:
    print(f"\n  Coarse types rejected:")
    for t, c in sorted(coarse_types.items(), key=lambda x: -x[1]):
        print(f"    {t}: {c}")

if precise_types:
    print(f"\n  Precise types accepted:")
    for t, c in sorted(precise_types.items(), key=lambda x: -x[1]):
        print(f"    {t}: {c}")

# Rejected detail
rej_df = old_geo[old_geo["Geocode_Source"].str.contains("too coarse", na=False)]
if len(rej_df) > 0:
    print(f"\n  REJECTED SCHOOLS ({len(rej_df)}):")
    for _, r in rej_df.iterrows():
        print(f"    {r['EMIS_Code']} | {str(r['School_Name'])[:50]:50s} | "
              f"{r['Geocode_Source']}")

print(f"\n{'='*80}")
print("NEXT: python fetch_landcover_grid.py, then merge_satellite_scores_v2.py")
print(f"{'='*80}")
