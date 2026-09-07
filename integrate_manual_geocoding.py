"""
integrate_manual_geocoding.py
=============================
Integrates manually-geocoded schools (Google Maps lookups recorded in
"schools_needing_manual_geocoding_READY (1).csv") into the GhostWatch pipeline.

What it does
------------
1. Reads the READY file and keeps only rows where Latitude/Longitude are
   filled (the manually geocoded schools).
2. Sanity-checks each coordinate against a Punjab (Pakistan) bounding box.
3. Snapshots every school's current Priority from
   ghostwatch_final_merged_v2.csv into _priority_before_manual_geo.csv
   (used after re-merging to report priority changes).
4. Runs the exact same 9x9 grid LULC sampling (2017 vs 2023) as
   fetch_landcover_grid.py -- the sampling functions are imported from
   resample_nodata_satellite.py so the logic is guaranteed identical.
5. Writes the new coordinates + satellite results into:
     - geocoded_schools.csv      (coordinate source for re-sampling)
     - satellite_results.csv     (input of fetch_landcover_grid.py)
     - satellite_results_v2.csv  (input of merge_satellite_scores_v2.py)
   All three files are re-saved after every school, so a network failure
   mid-run never loses progress.

What it does NOT do
-------------------
It does not touch ghostwatch_final_merged_v2.csv.  After this script, run
the standard pipeline steps:
    python merge_satellite_scores_v2.py
    python add_explanation_columns.py
    python build_final_reason_and_map.py

RUN: python integrate_manual_geocoding.py
"""

import sys
import time
from pathlib import Path

import pandas as pd

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Same sampling logic as fetch_landcover_grid.py (imported, not copied)
from resample_nodata_satellite import (
    sample_lulc_grid,
    classify_density,
    EARLIEST_YEAR,
    RECENT_YEAR,
    GRID_SIZE,
)

ROOT = Path(__file__).resolve().parent
READY_CSV = ROOT / "schools_needing_manual_geocoding_READY (1).csv"
GEOCODED_CSV = ROOT / "geocoded_schools.csv"
SAT_V1_CSV = ROOT / "satellite_results.csv"
SAT_V2_CSV = ROOT / "satellite_results_v2.csv"
FINAL_CSV = ROOT / "ghostwatch_final_merged_v2.csv"
SNAPSHOT_CSV = ROOT / "_priority_before_manual_geo.csv"

# Punjab (Pakistan) bounding box -- coarse sanity check for manual coordinates
LAT_MIN, LAT_MAX = 27.5, 34.5
LON_MIN, LON_MAX = 69.0, 75.5


def main():
    print("=" * 80)
    print("GhostWatch - Integrate Manually Geocoded Schools (Google Maps)")
    print("=" * 80)

    # ---- Step 1: load READY file, keep rows with coordinates -------------
    ready = pd.read_csv(READY_CSV, dtype={"EMIS_Code": str})
    manual = ready[ready["Latitude"].notna() & ready["Longitude"].notna()].copy()
    print(f"\nREADY file: {len(ready)} schools, "
          f"{len(manual)} with manual coordinates")
    if manual.empty:
        print("Nothing to do.")
        return

    # ---- Step 2: sanity-check coordinates --------------------------------
    ok_rows = []
    for _, r in manual.iterrows():
        lat, lon = float(r["Latitude"]), float(r["Longitude"])
        if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
            print(f"  REJECTED {r['EMIS_Code']} ({r['School_Name']}): "
                  f"({lat}, {lon}) outside Punjab bounding box")
            continue
        ok_rows.append(r)
    manual = pd.DataFrame(ok_rows).reset_index(drop=True)
    print(f"Coordinates within Punjab bounds: {len(manual)}")
    if manual.empty:
        print("Nothing to do.")
        return

    # ---- Step 3: snapshot current priorities (the 'before' state) --------
    final = pd.read_csv(FINAL_CSV, dtype={"EMIS_Code": str})
    snap_cols = ["EMIS_Code", "School_Name", "Tier", "Ghost_Risk_Score_Tier1",
                 "Infrastructure_Screening_Score", "Priority"]
    final[snap_cols].to_csv(SNAPSHOT_CSV, index=False)
    print(f"Priority snapshot saved: {SNAPSHOT_CSV.name} ({len(final)} rows)")

    # ---- Step 4: load the three coordinate-bearing files -----------------
    geocoded = pd.read_csv(GEOCODED_CSV, dtype={"EMIS_Code": str})
    sat_v1 = pd.read_csv(SAT_V1_CSV, dtype={"EMIS_Code": str})
    sat_v2 = pd.read_csv(SAT_V2_CSV, dtype={"EMIS_Code": str})

    v2_emis = set(sat_v2["EMIS_Code"])
    missing = [c for c in manual["EMIS_Code"] if c not in v2_emis]
    if missing:
        print(f"WARNING: EMIS codes not found in satellite_results_v2.csv: "
              f"{missing} (they will be skipped)")
        manual = manual[manual["EMIS_Code"].isin(v2_emis)].reset_index(drop=True)

    # ---- Step 5: sample + write results ----------------------------------
    print(f"\nSampling {GRID_SIZE}x{GRID_SIZE} grid for {len(manual)} schools "
          f"({EARLIEST_YEAR} vs {RECENT_YEAR})...\n")

    recovered = 0
    for i, (_, r) in enumerate(manual.iterrows(), 1):
        emis = r["EMIS_Code"]
        lat, lon = float(r["Latitude"]), float(r["Longitude"])
        print(f"[{i}/{len(manual)}] {emis} ({r['School_Name']}) "
              f"@ ({lat:.6f}, {lon:.6f})")

        print(f"    {EARLIEST_YEAR} ...", end=" ", flush=True)
        res_e = sample_lulc_grid(lat, lon, EARLIEST_YEAR)
        if res_e:
            print(f"{res_e['percent_built']:.1f}% "
                  f"({res_e['n_samples']} px, tile={res_e.get('tile', '?')})")
        else:
            print("No data")
        time.sleep(0.5)

        print(f"    {RECENT_YEAR} ...", end=" ", flush=True)
        res_r = sample_lulc_grid(lat, lon, RECENT_YEAR)
        if res_r:
            print(f"{res_r['percent_built']:.1f}% "
                  f"({res_r['n_samples']} px, tile={res_r.get('tile', '?')})")
        else:
            print("No data")
        time.sleep(0.5)

        pe = res_e["percent_built"] if res_e else None
        pr = res_r["percent_built"] if res_r else None
        sp = pr if pr is not None else pe
        density = classify_density(sp)

        # Coordinates go into all three files
        for df in (geocoded, sat_v1, sat_v2):
            mask = df["EMIS_Code"] == emis
            df.loc[mask, "Latitude"] = lat
            df.loc[mask, "Longitude"] = lon
        geocoded.loc[geocoded["EMIS_Code"] == emis,
                     "Geocode_Source"] = "Manual (Google Maps)"

        # Satellite columns only exist in satellite_results_v2.csv
        mask_v2 = sat_v2["EMIS_Code"] == emis
        sat_v2.loc[mask_v2, "Percent_Built_Earliest"] = pe
        sat_v2.loc[mask_v2, "Percent_Built_Recent"] = pr
        sat_v2.loc[mask_v2, "Satellite_BuiltUp_Percent"] = sp
        sat_v2.loc[mask_v2, "Satellite_Density_Flag"] = density

        if sp is not None:
            print(f"    -> {sp}% built-up, {density}")
            recovered += 1
        else:
            print("    -> still NoData (coordinates kept, no modifier)")

        # Save after every school so progress is never lost
        geocoded.to_csv(GEOCODED_CSV, index=False)
        sat_v1.to_csv(SAT_V1_CSV, index=False)
        sat_v2.to_csv(SAT_V2_CSV, index=False)

    # ---- Step 6: summary ---------------------------------------------------
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Schools processed        : {len(manual)}")
    print(f"Satellite data obtained  : {recovered}")
    print(f"Still NoData             : {len(manual) - recovered}")
    print(f"geocoded_schools.csv     : "
          f"{geocoded['Latitude'].notna().sum()} with coords")
    print(f"satellite_results.csv    : "
          f"{sat_v1['Latitude'].notna().sum()} with coords")
    print(f"satellite_results_v2.csv : "
          f"{sat_v2['Latitude'].notna().sum()} with coords")
    print(f"  density flags for these schools:")
    for _, r in manual.iterrows():
        row = sat_v2[sat_v2["EMIS_Code"] == r["EMIS_Code"]].iloc[0]
        print(f"    {r['EMIS_Code']} | {str(r['School_Name'])[:40]:40s} | "
              f"{row['Satellite_BuiltUp_Percent']}% | "
              f"{row['Satellite_Density_Flag']}")

    print("\nNext steps (standard pipeline):")
    print("  python merge_satellite_scores_v2.py")
    print("  python add_explanation_columns.py")
    print("  python build_final_reason_and_map.py")


if __name__ == "__main__":
    main()
