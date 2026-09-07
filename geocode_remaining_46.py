"""
geocode_remaining_46.py
========================
Geocode the 46 real schools that have no coordinates.

Strategy:
  1. Try Nominatim (free OpenStreetMap geocoder) with school name + tehsil + district
  2. If Nominatim returns a precise result (street/building level), accept it
  3. If Nominatim returns coarse (city/district) or nothing, fall back to
     the median coordinate of already-geocoded schools in the SAME tehsil
  4. Mark coordinate source: "Nominatim" or "Tehsil-Centroid-Fallback"

After geocoding, the script:
  - Updates ghostwatch_final_merged_v2.csv with new coordinates
  - Sets Satellite_Confidence = "Tehsil-Level" for tehsil-fallback schools
  - Prints a summary of results

THEN run fetch_hd_satellite_images.py to get images for the newly geocoded schools.
"""

import os
import sys
import time
import json
import urllib.request
import urllib.parse
import math
import pandas as pd
import numpy as np
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
CSV = ROOT / "ghostwatch_final_merged_v2.csv"

# Nominatim geocoding
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Result types we consider "precise enough"
PRECISE_TYPES = {
    "amenity", "building", "school", "place_of_worship",
    "residential", "house", "address", "place",
    "hamlet", "village", "neighbourhood", "suburb",
}

# Result types that are too coarse
COARSE_TYPES = {
    "administrative", "city", "town", "state", "county",
    "district", "region", "country", "province", "political",
}


def nominatim_search(query, lat=None, lon=None):
    """
    Search Nominatim for a query string.
    Returns (lat, lon, type_str, display_name) or None.
    """
    params = {
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 3,
        "countrycodes": "pk",
    }
    if lat and lon:
        params["viewbox"] = f"{lon-0.5},{lat+0.5},{lon+0.5},{lat-0.5}"
        params["bounded"] = 1

    url = f"{NOMINATIM_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "GhostWatch/1.0 (ghostwatch@edu.pk)"
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            results = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return None

    if not results:
        return None

    # Try to find the most precise result
    for r in results:
        rtype = r.get("type", "").lower()
        rclass = r.get("class", "").lower()
        display = r.get("display_name", "")
        rlat = float(r.get("lat", 0))
        rlon = float(r.get("lon", 0))

        # Check if precise enough
        if rtype in PRECISE_TYPES or rclass in PRECISE_TYPES:
            return (rlat, rlon, rtype, display)

        # Check importance score (0-1, higher = more precise)
        importance = float(r.get("importance", 0))
        if importance < 0.3:
            return (rlat, rlon, rtype, display)

    # Return first result even if coarse
    r = results[0]
    return (
        float(r.get("lat", 0)),
        float(r.get("lon", 0)),
        r.get("type", "unknown"),
        r.get("display_name", ""),
    )


def compute_tehsil_centroids(df):
    """Compute median lat/lon for each tehsil from already-geocoded schools."""
    geocoded = df[df["Latitude"].notna() & df["Longitude"].notna()].copy()
    centroids = {}
    for (district, tehsil), group in geocoded.groupby(["District", "Tehsil"]):
        centroids[(district, tehsil)] = (
            group["Latitude"].median(),
            group["Longitude"].median(),
        )
    return centroids


def main():
    print("=" * 65)
    print("Geocoding 46 Schools Without Coordinates")
    print("=" * 65)

    df = pd.read_csv(CSV, dtype={"EMIS_Code": str})

    # Identify schools without coordinates (real, not synthetic)
    no_coords = df[
        df["Latitude"].isna() &
        (df["Data_Type"].astype(str) != "Synthetic")
    ]
    print(f"Schools without coordinates: {len(no_coords)}")

    # Compute tehsil centroids from existing geocoded schools
    centroids = compute_tehsil_centroids(df)
    print(f"Tehsil centroids available: {len(centroids)}")

    results = {"nominatim": [], "tehsil_fallback": [], "failed": []}

    for idx, (_, row) in enumerate(no_coords.iterrows()):
        emis = row["EMIS_Code"]
        name = row.get("School_Name", "Unknown")
        district = row.get("District", "")
        tehsil = row.get("Tehsil", "")

        print(f"\n  [{idx+1}/{len(no_coords)}] {emis}: {name}")
        print(f"    District: {district}, Tehsil: {tehsil}")

        # Build search query
        # Remove common prefixes like GPS, GGPS, GGHS, GHS, etc.
        clean_name = name
        for prefix in ["GGHSS", "GGHS", "GGCMS", "GGES", "GGPS", "GGMPS",
                       "GMMS", "GMPS", "GHS", "GPS", "GES", "GMS"]:
            if clean_name.startswith(prefix + " "):
                clean_name = clean_name[len(prefix):]
                break

        query = f"{clean_name} school, {tehsil}, {district}, Punjab, Pakistan"
        print(f"    Query: {query}")

        # Get a rough hint from tehsil centroid for viewbox bias
        hint_lat, hint_lon = None, None
        key = (district, tehsil)
        if key in centroids:
            hint_lat, hint_lon = centroids[key]

        # Try Nominatim
        result = nominatim_search(query, hint_lat, hint_lon)
        time.sleep(1.2)  # Nominatim rate limit: 1 req/sec

        if result:
            lat, lon, rtype, display = result
            is_precise = rtype not in COARSE_TYPES

            # Check distance from tehsil centroid (if available)
            dist_km = None
            if hint_lat and hint_lon:
                dlat = lat - hint_lat
                dlon = (lon - hint_lon) * math.cos(math.radians(hint_lat))
                dist_km = math.sqrt(dlat**2 + dlon**2) * 111.32

            print(f"    Nominatim: ({lat:.4f}, {lon:.4f}) type={rtype}")
            print(f"    Display: {display[:80]}")
            if dist_km is not None:
                print(f"    Distance from tehsil center: {dist_km:.1f} km")

            # Accept if precise type AND within reasonable distance
            accept = False
            if is_precise and dist_km is not None and dist_km < 30:
                accept = True
                source = "Nominatim"
            elif is_precise and dist_km is None:
                accept = True
                source = "Nominatim"

            if accept:
                df.loc[df["EMIS_Code"] == emis, "Latitude"] = lat
                df.loc[df["EMIS_Code"] == emis, "Longitude"] = lon
                df.loc[df["EMIS_Code"] == emis, "Satellite_Confidence"] = "School-Level"
                results["nominatim"].append((emis, name, lat, lon, rtype))
                print(f"    -> ACCEPTED ({source}, type={rtype})")
                continue
            else:
                print(f"    -> Rejected (type={rtype}, dist={dist_km})")

        # Fallback: tehsil centroid
        if key in centroids:
            lat, lon = centroids[key]
            df.loc[df["EMIS_Code"] == emis, "Latitude"] = lat
            df.loc[df["EMIS_Code"] == emis, "Longitude"] = lon
            df.loc[df["EMIS_Code"] == emis, "Satellite_Confidence"] = "Tehsil-Level"
            results["tehsil_fallback"].append((emis, name, lat, lon, tehsil))
            print(f"    -> TEHSIL FALLBACK: ({lat:.4f}, {lon:.4f})")
        else:
            # Try district-level centroid
            dist_centroids = {
                k: v for k, v in centroids.items() if k[0] == district
            }
            if dist_centroids:
                lats = [v[0] for v in dist_centroids.values()]
                lons = [v[1] for v in dist_centroids.values()]
                lat, lon = np.median(lats), np.median(lons)
                df.loc[df["EMIS_Code"] == emis, "Latitude"] = lat
                df.loc[df["EMIS_Code"] == emis, "Longitude"] = lon
                df.loc[df["EMIS_Code"] == emis, "Satellite_Confidence"] = "Tehsil-Level"
                results["tehsil_fallback"].append((emis, name, lat, lon, district))
                print(f"    -> DISTRICT FALLBACK: ({lat:.4f}, {lon:.4f})")
            else:
                results["failed"].append((emis, name, district))
                print(f"    -> NO FALLBACK AVAILABLE")

    # Summary
    print(f"\n{'=' * 65}")
    print("SUMMARY")
    print("=" * 65)
    print(f"  Nominatim geocoded:     {len(results['nominatim'])}")
    print(f"  Tehsil/District fallback: {len(results['tehsil_fallback'])}")
    print(f"  Failed (no fallback):   {len(results['failed'])}")

    if results["nominatim"]:
        print("\n  Nominatim matches:")
        for emis, name, lat, lon, rtype in results["nominatim"]:
            print(f"    {emis}: {name} -> ({lat:.4f}, {lon:.4f}) [{rtype}]")

    if results["failed"]:
        print("\n  Failed:")
        for emis, name, district in results["failed"]:
            print(f"    {emis}: {name} ({district})")

    # Save updated CSV
    df.to_csv(CSV, index=False)
    print(f"\nUpdated CSV saved to {CSV}")
    print(f"Schools with coordinates now: {df['Latitude'].notna().sum()}/{len(df)}")


if __name__ == "__main__":
    main()
