"""
geoapify_fallback_geocode.py
=============================
Fallback geocoding for schools that failed Nominatim precision validation.

Uses Geoapify free-tier API to retry geocoding for the ~57 schools that
Nominatim could not precisely locate, then runs 9x9 grid satellite sampling
(via Microsoft Planetary Computer) on any newly geocoded schools, and
merges everything back into ghostwatch_final_merged_v2.csv.

SETUP:
    pip install requests pandas numpy rasterio planetary-computer pystac-client
    set GEOAPIFY_API_KEY=your_key_here

RUN:
    python geoapify_fallback_geocode.py
"""

import os
import sys
import time
import requests
import numpy as np
import pandas as pd
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

GEOAPIFY_API_KEY = os.environ.get("GEOAPIFY_API_KEY", "")

ROOT = Path(__file__).resolve().parent
GEOCODED_CSV     = ROOT / "geocoded_schools.csv"
FINAL_CSV        = ROOT / "ghostwatch_final_merged_v2.csv"
SAT_RESULTS_CSV  = ROOT / "satellite_results.csv"
SAT_V2_CSV       = ROOT / "satellite_results_v2.csv"
TIER_SCORES_CSV  = ROOT / "ghostwatch_two_tier_scores.csv"

COARSE_REJECT_TYPES = {
    "administrative", "boundary", "political",
    "city", "town", "state", "county", "district", "region",
    "country", "continent", "municipality", "province",
    "municipality_district", "region_part", "city_district",
    "borough", "suburb",
}

GRID_SIZE      = 9
PIXEL_SIZE_M   = 10
WINDOW_SIZE_M  = (GRID_SIZE - 1) * PIXEL_SIZE_M
BUILT_AREA_CLASS = 7
EARLIEST_YEAR  = 2017
RECENT_YEAR    = 2023
STAC_API_URL   = "https://planetarycomputer.microsoft.com/api/stac/v1"


def build_query(row):
    parts = [
        str(row.get("School_Name", "")),
        str(row.get("Tehsil", "")),
        str(row.get("District", "")),
        "Punjab, Pakistan",
    ]
    return ", ".join(p for p in parts if p and p.lower() != "nan")


def geocode_geoapify(query):
    try:
        resp = requests.get(
            "https://api.geoapify.com/v1/geocode/search",
            params={
                "text": query,
                "apiKey": GEOAPIFY_API_KEY,
                "limit": 5,
                "format": "json",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        features = data.get("features", [])

        for feat in features:
            props = feat.get("properties", {})
            result_type = (props.get("result_type") or "").lower().strip()
            lon, lat = feat["geometry"]["coordinates"]
            confidence = props.get("confidence", 0)

            if result_type in COARSE_REJECT_TYPES:
                print(f"    Geoapify: '{result_type}' (too coarse, "
                      f"conf={confidence:.2f}), trying next...")
                continue

            return lat, lon, result_type, confidence

        if features:
            types = [f["properties"].get("result_type", "?")
                     for f in features]
            print(f"    All Geoapify results coarse: {types}")
        return None, None, None, None

    except Exception as e:
        print(f"    Geoapify API error: {e}")
        return None, None, None, None


def _check_pc_imports():
    try:
        import planetary_computer
        import pystac_client
        return True
    except ImportError:
        return False


def query_and_sign_items(year, bbox):
    import pystac_client
    import planetary_computer

    start_date = f"{year}-01-01"
    end_date = f"{year}-12-31"
    try:
        catalog = pystac_client.Client.open(STAC_API_URL)
        search = catalog.search(
            collections=["io-lulc-annual-v02"],
            datetime=f"{start_date}/{end_date}",
            bbox=bbox,
            limit=10,
        )
        items = list(search.items())
        if not items:
            return []
        return [planetary_computer.sign(item) for item in items]
    except Exception as e:
        print(f"    STAC query failed: {e}")
        return []


def sample_lulc_grid(lat, lon, year):
    import rasterio
    from rasterio.warp import transform as warp_transform
    from rasterio.windows import from_bounds

    search_offset = 0.01
    search_bbox = [
        lon - search_offset, lat - search_offset,
        lon + search_offset, lat + search_offset,
    ]

    items = query_and_sign_items(year, search_bbox)
    if not items:
        return None

    # Try ALL tiles - some may be edge tiles with no data coverage
    for item in items:
        asset = item.assets.get("data")
        if not asset:
            continue

        try:
            with rasterio.open(asset.href) as src:
                xs, ys = warp_transform(
                    "EPSG:4326", src.crs, [lon], [lat])
                x_m, y_m = xs[0], ys[0]

                half = WINDOW_SIZE_M / 2
                window = from_bounds(
                    x_m - half, y_m - half,
                    x_m + half, y_m + half,
                    src.transform,
                )
                data = src.read(
                    1, window=window, boundless=True, fill_value=0)

                pixels = [p for p in data.flatten().tolist()
                          if p != 0]
                if not pixels:
                    continue  # try next tile

                class_counts = {}
                for p in pixels:
                    class_counts[p] = class_counts.get(p, 0) + 1

                built_count = class_counts.get(BUILT_AREA_CLASS, 0)
                percent_built = (built_count / len(pixels)) * 100.0

                return {
                    "percent_built": round(percent_built, 2),
                    "class_counts": class_counts,
                    "n_samples": len(pixels),
                }
        except Exception as e:
            print(f"    COG read failed ({item.id}): {e}")
            continue

    return None


def classify_density(percent_built):
    if percent_built is None or pd.isna(percent_built):
        return "NoData"
    if percent_built < 30:
        return "Low_Density"
    elif percent_built <= 70:
        return "Mixed_Density"
    else:
        return "High_Density"


def remerge_final_csv():
    scores = pd.read_csv(TIER_SCORES_CSV, dtype={"EMIS_Code": str})
    satellite = pd.read_csv(SAT_V2_CSV, dtype={"EMIS_Code": str})

    sat_cols = satellite[
        ["EMIS_Code", "Percent_Built_Earliest", "Percent_Built_Recent",
         "Satellite_BuiltUp_Percent", "Satellite_Density_Flag",
         "Latitude", "Longitude"]
    ].copy()

    merged = scores.merge(sat_cols, on="EMIS_Code", how="left")

    def _active_base_score(row):
        tier1 = row.get("Ghost_Risk_Score_Tier1")
        if pd.notna(tier1) and str(tier1).strip() != "":
            return float(tier1)
        tier2 = row.get("Infrastructure_Screening_Score")
        if pd.notna(tier2) and str(tier2).strip() != "":
            return float(tier2)
        return 0.0

    merged["_base_score"] = merged.apply(_active_base_score, axis=1)

    SAT_MOD = {
        "Low_Density": 15, "Mixed_Density": 7,
        "High_Density": 0, "NoData": 0,
    }

    def _sat_mod(flag):
        if pd.isna(flag):
            return 0
        return SAT_MOD.get(str(flag).strip(), 0)

    merged["_sat_mod"] = merged["Satellite_Density_Flag"].apply(_sat_mod)
    merged["_adjusted"] = (
        merged["_base_score"] + merged["_sat_mod"]
    ).clip(upper=100)

    def _priority(score):
        if score >= 50:
            return "High"
        if score >= 20:
            return "Medium"
        return "Low"

    merged["Priority"] = merged["_adjusted"].apply(_priority)

    esc = {"Low": "Medium", "Medium": "High", "High": "High"}
    ill_mask = (
        merged["Building_Illegal_Occupation"]
        .astype(str).str.strip().str.lower() == "yes"
    )
    merged.loc[ill_mask, "Priority"] = (
        merged.loc[ill_mask, "Priority"].map(esc)
    )

    fraud_mask = (
        merged["Qwen_Fraud_Flag"].astype(str).str.strip() == "True"
    )
    fraud_esc = {"Low": "Low", "Medium": "High", "High": "High"}
    merged.loc[fraud_mask, "Priority"] = (
        merged.loc[fraud_mask, "Priority"].map(fraud_esc)
    )

    mask_t1 = merged["Ghost_Risk_Score_Tier1"].notna() & (
        merged["Ghost_Risk_Score_Tier1"].astype(str).str.strip() != ""
    )
    mask_t2 = ~mask_t1
    merged.loc[mask_t1, "Ghost_Risk_Score_Tier1"] = (
        merged.loc[mask_t1, "_adjusted"].round(2)
    )
    merged.loc[mask_t2, "Infrastructure_Screening_Score"] = (
        merged.loc[mask_t2, "_adjusted"].round(2)
    )

    syn_mask = (
        merged["Data_Type"].astype(str).str.strip() == "Synthetic"
    )
    for col in ["Percent_Built_Earliest", "Percent_Built_Recent",
                "Satellite_BuiltUp_Percent", "Satellite_Density_Flag"]:
        if col in merged.columns:
            merged.loc[syn_mask, col] = None
    for col in ["Latitude", "Longitude"]:
        if col not in merged.columns:
            merged[col] = None
        merged.loc[syn_mask, col] = None

    merged.drop(
        columns=["_base_score", "_sat_mod", "_adjusted"], inplace=True
    )
    merged.to_csv(FINAL_CSV, index=False)
    return merged


def main():
    print("=" * 80)
    print("GhostWatch - Geoapify Fallback Geocoding + Satellite Sampling")
    print("=" * 80)

    if not GEOAPIFY_API_KEY:
        print("\nERROR: GEOAPIFY_API_KEY not set!")
        print("  Windows:  set GEOAPIFY_API_KEY=your_key_here")
        sys.exit(1)
    print(f"  Geoapify API key: {GEOAPIFY_API_KEY[:6]}..."
          f"{GEOAPIFY_API_KEY[-4:]}")

    has_pc = _check_pc_imports()
    if has_pc:
        print("  Planetary Computer SDK: OK")
    else:
        print("  WARNING: planetary-computer/pystac-client not installed.")
        print("  Satellite sampling will be SKIPPED. Install with:")
        print("    pip install planetary-computer pystac-client rasterio")

    geo_df = pd.read_csv(GEOCODED_CSV, dtype={"EMIS_Code": str})
    final_df = pd.read_csv(FINAL_CSV, dtype={"EMIS_Code": str})
    final_lookup = final_df.set_index("EMIS_Code").to_dict("index")

    failed_mask = geo_df["Geocode_Source"].str.contains(
        "Not Geocoded", na=False)
    synthetic_mask = geo_df["School_Name"].str.contains(
        "Synthetic", na=False)
    target_mask = failed_mask & ~synthetic_mask
    targets = geo_df[target_mask].copy()
    n_targets = len(targets)

    sat_flags = final_df["Satellite_Density_Flag"]
    old_with_sat = sat_flags.isin(
        ["Low_Density", "Mixed_Density", "High_Density"]).sum()
    old_nodata = (sat_flags == "NoData").sum()

    print(f"\nSchools that failed Nominatim    : {n_targets}")
    print(f"Schools with satellite data     : {old_with_sat}")
    print(f"Schools with NoData satellite   : {old_nodata}")

    # == STEP 1: Geoapify geocoding =========================================
    print(f"\n{'='*80}")
    print(f"STEP 1: Geoapify fallback geocoding ({n_targets} schools)")
    print(f"{'='*80}\n")

    newly_geocoded = {}

    for i, (idx, row) in enumerate(targets.iterrows(), 1):
        emis = row["EMIS_Code"]
        name = row["School_Name"]

        fin = final_lookup.get(emis, {})
        query_row = {
            "School_Name": name,
            "Tehsil": fin.get("Tehsil", ""),
            "District": fin.get("District", ""),
        }
        query = build_query(query_row)

        print(f"  [{i}/{n_targets}] {emis} - {name}")
        print(f"    Query: {query}")

        lat, lon, rtype, conf = geocode_geoapify(query)

        if lat is not None:
            print(f"    MATCH: {rtype} (conf={conf:.2f}) -> "
                  f"({lat:.6f}, {lon:.6f})")
            newly_geocoded[emis] = (lat, lon, rtype)
        else:
            print(f"    NO MATCH")

        time.sleep(0.3)

    n_found = len(newly_geocoded)
    n_failed = n_targets - n_found

    print(f"\nGeoapify results:")
    print(f"  Precise matches found : {n_found}")
    print(f"  Still no match        : {n_failed}")

    if n_found == 0:
        print("\nNo new schools geocoded. Nothing more to do.")
        return

    # == STEP 2: Update CSV files ===========================================
    print(f"\n{'='*80}")
    print("STEP 2: Updating geocoded_schools.csv and satellite_results.csv")
    print(f"{'='*80}\n")

    for emis, (lat, lon, rtype) in newly_geocoded.items():
        mask = geo_df["EMIS_Code"] == emis
        geo_df.loc[mask, "Latitude"] = lat
        geo_df.loc[mask, "Longitude"] = lon
        geo_df.loc[mask, "Geocode_Source"] = f"Geoapify ({rtype})"

    geo_df.to_csv(GEOCODED_CSV, index=False)
    print(f"  Updated {GEOCODED_CSV}")

    sat_df = pd.read_csv(SAT_RESULTS_CSV, dtype={"EMIS_Code": str})
    for emis, (lat, lon, rtype) in newly_geocoded.items():
        mask = sat_df["EMIS_Code"] == emis
        sat_df.loc[mask, "Latitude"] = lat
        sat_df.loc[mask, "Longitude"] = lon
    sat_df.to_csv(SAT_RESULTS_CSV, index=False)
    print(f"  Updated {SAT_RESULTS_CSV}")

    # == STEP 3: Satellite grid sampling ====================================
    if not has_pc:
        print("\nSkipping satellite sampling (SDK not available).")
        return

    print(f"\n{'='*80}")
    print(f"STEP 3: 9x9 grid satellite sampling ({n_found} schools)")
    print(f"{'='*80}\n")

    sat_v2_df = pd.read_csv(SAT_V2_CSV, dtype={"EMIS_Code": str})

    for i, (emis, (lat, lon, rtype)) in enumerate(
            newly_geocoded.items(), 1):
        name = final_lookup.get(emis, {}).get(
            "School_Name", "Unknown")
        print(f"  [{i}/{n_found}] {emis} ({name})...")

        print(f"    Earliest ({EARLIEST_YEAR})...", end=" ")
        res_e = sample_lulc_grid(lat, lon, EARLIEST_YEAR)
        if res_e:
            print(f"{res_e['percent_built']:.1f}% built-up "
                  f"({res_e['n_samples']} px)")
        else:
            print("No data")
        time.sleep(0.5)

        print(f"    Recent  ({RECENT_YEAR})...", end=" ")
        res_r = sample_lulc_grid(lat, lon, RECENT_YEAR)
        if res_r:
            print(f"{res_r['percent_built']:.1f}% built-up "
                  f"({res_r['n_samples']} px)")
        else:
            print("No data")
        time.sleep(0.5)

        pct_e = res_e["percent_built"] if res_e else None
        pct_r = res_r["percent_built"] if res_r else None
        sat_pct = pct_r if pct_r is not None else pct_e
        density = classify_density(sat_pct)

        mask = sat_v2_df["EMIS_Code"] == emis
        sat_v2_df.loc[mask, "Latitude"] = lat
        sat_v2_df.loc[mask, "Longitude"] = lon
        sat_v2_df.loc[mask, "Percent_Built_Earliest"] = pct_e
        sat_v2_df.loc[mask, "Percent_Built_Recent"] = pct_r
        sat_v2_df.loc[mask, "Satellite_BuiltUp_Percent"] = sat_pct
        sat_v2_df.loc[mask, "Satellite_Density_Flag"] = density

        print(f"    -> BuiltUp={sat_pct}, Density={density}")

    sat_v2_df.to_csv(SAT_V2_CSV, index=False)
    print(f"\n  Updated {SAT_V2_CSV}")

    # == STEP 4: Re-merge ===================================================
    print(f"\n{'='*80}")
    print("STEP 4: Re-merging into ghostwatch_final_merged_v2.csv")
    print(f"{'='*80}\n")

    merged = remerge_final_csv()
    print(f"  Saved: {FINAL_CSV}  ({len(merged)} rows)")

    # == STEP 5: Coverage report ============================================
    print(f"\n{'='*80}")
    print("COVERAGE REPORT")
    print(f"{'='*80}\n")

    syn_mask = (
        merged["Data_Type"].astype(str).str.strip() == "Synthetic")
    total_real = len(merged) - syn_mask.sum()

    with_sat = merged[merged["Satellite_Density_Flag"].isin(
        ["Low_Density", "Mixed_Density", "High_Density"])]
    n_with_sat = len(with_sat)
    n_nodata = (merged["Satellite_Density_Flag"] == "NoData").sum()
    coverage_pct = (n_with_sat / total_real) * 100

    print(f"  NEW Geoapify precise matches : "
          f"{n_found} / {n_targets} failed schools")
    print(f"  Still un-geocoded            : {n_failed}")
    print()
    print(f"  Schools with satellite data  : "
          f"{n_with_sat} / {total_real} ({coverage_pct:.1f}%)")
    print(f"  Schools still NoData         : {n_nodata}")
    print(f"  (Previously: {old_with_sat} / {total_real})")
    print(f"  Net gain: +{n_with_sat - old_with_sat} schools")
    print()

    print("  NEWLY GEOCODED SCHOOLS:")
    hdr = (f"  {'EMIS':>10s}  {'School Name':40s}  "
           f"{'Type':16s}  {'BuiltUp%':>8s}  {'Density':12s}")
    print(hdr)
    print("  " + "-" * 92)
    for emis in newly_geocoded:
        row = merged[merged["EMIS_Code"] == emis]
        if row.empty:
            continue
        r = row.iloc[0]
        sp = r.get("Satellite_BuiltUp_Percent", None)
        df = r.get("Satellite_Density_Flag", "")
        sp_s = f"{sp:.1f}" if pd.notna(sp) else "N/A"
        rt = newly_geocoded[emis][2]
        print(f"  {emis:>10s}  "
              f"{str(r['School_Name'])[:40]:40s}  "
              f"{rt:16s}  {sp_s:>8s}  {str(df):12s}")

    if n_failed > 0:
        print(f"\n  STILL FAILED ({n_failed} schools):")
        still = geo_df[
            geo_df["Geocode_Source"].str.contains(
                "Not Geocoded", na=False)
            & ~geo_df["School_Name"].str.contains(
                "Synthetic", na=False)
        ]
        for _, r in still.iterrows():
            fin = final_lookup.get(r["EMIS_Code"], {})
            print(f"    {r['EMIS_Code']} | "
                  f"{str(r['School_Name'])[:45]:45s} | "
                  f"{fin.get('District', '?')}, "
                  f"{fin.get('Tehsil', '?')}")

    print(f"\n  Density distribution (real schools only):")
    real_m = merged[~syn_mask]
    for flag in ["High_Density", "Mixed_Density",
                 "Low_Density", "NoData"]:
        count = (real_m["Satellite_Density_Flag"] == flag).sum()
        pct = (count / total_real) * 100
        print(f"    {flag:15s}: {count:3d} ({pct:5.1f}%)")

    print(f"\n{'='*80}")
    print("DONE.")
    if n_failed > 0:
        print(f"  {n_failed} rural schools could not be geocoded by either")
        print(f"  Nominatim or Geoapify. These are typically small")
        print(f"  'Chak X' villages in southern Punjab lacking structured")
        print(f"  addresses in free geocoding databases.")
    else:
        print(f"  All {n_targets} previously-failed schools geocoded!")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()
