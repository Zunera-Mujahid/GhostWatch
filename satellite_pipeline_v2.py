"""
GhostWatch — Satellite Cross-Reference Pipeline (v2: Fully Automated Geocoding)
=================================================================================
Purpose: Take schools from the PMIU ground-truth dataset (EMIS code + address
fields), geocode them with a MULTI-PROVIDER FALLBACK CHAIN (so no school is
left needing a manual Google Maps lookup), then pull Sentinel-2 satellite
imagery to compute a Built-up Activity Index. Feed that as an extra feature
alongside the Government_Monitoring_Risk_Score for the final ghost-school
confidence score.

WHAT CHANGED FROM v1
---------------------
v1 only tried Nominatim (OpenStreetMap). Small "Chak X GB" villages often
aren't in OpenStreetMap, so v1 could leave some schools with no coordinates,
requiring a human to manually check Google Maps.

v2 tries THREE geocoders in order, automatically, per school:
    1. Nominatim       (free, no key, but weak coverage for tiny villages)
    2. Geoapify        (free tier, no credit card, better coverage)
    3. Google Geocoding API (best coverage, requires a Google Cloud API key
       + billing account — optional; script skips this step gracefully if
       no key is configured)

Only if ALL THREE fail does a school end up with no coordinates — and even
then, the script does NOT ask a human to go check Maps. It simply marks that
school "Location_Status = Not Geocoded" in the output and moves on. The
ground-monitoring risk score (from PMIU data) is still used for that school;
it just won't have a satellite cross-check. No manual step is required at
any point.

SETUP (run once, outside this sandbox — on your own machine):
    pip install geopy requests earthengine-api pandas

    # Google Earth Engine needs a free account + one-time auth:
    #   1. Sign up: https://signup.earthengine.google.com/
    #   2. Run: earthengine authenticate   (opens browser, one-time)

    # Geoapify (recommended, free, no credit card):
    #   1. Sign up: https://www.geoapify.com/
    #   2. Copy your free API key into GEOAPIFY_API_KEY below (or set the
    #      GEOAPIFY_API_KEY environment variable instead of hardcoding it)

    # Google Geocoding API (optional, skip if you don't want to set up
    # billing — the pipeline works fine with just Nominatim + Geoapify):
    #   1. https://console.cloud.google.com/ -> enable "Geocoding API"
    #   2. Create an API key, paste into GOOGLE_GEOCODING_API_KEY below

Then run:
    python satellite_pipeline_v2.py ghost_school_detector_full_dataset.csv
"""

import os
import time
import requests
import pandas as pd
from geopy.geocoders import Nominatim

# ---- CONFIG ----------------------------------------------------------------
# Put your free Geoapify key here (or leave blank and set env var instead).
GEOAPIFY_API_KEY = os.environ.get("GEOAPIFY_API_KEY", "")

# Optional — only needed if you set up Google Cloud billing. Leave blank to
# skip this provider entirely (the pipeline still works with the other two).
GOOGLE_GEOCODING_API_KEY = os.environ.get("GOOGLE_GEOCODING_API_KEY", "")


# ---- STEP 1: Multi-provider geocoding with automatic fallback -------------

def build_query(row):
    parts = [
        row.get("School_Name", ""),
        row.get("Mauza", ""),      # if you've added this column, great — more precise
        row.get("UC_Name", ""),    # same
        row.get("Tehsil", ""),
        row.get("District", ""),
        "Punjab, Pakistan",
    ]
    return ", ".join([str(p) for p in parts if p and str(p) != "nan"])


def try_nominatim(query, geolocator):
    try:
        location = geolocator.geocode(query, timeout=10)
        if location:
            return location.latitude, location.longitude
    except Exception as e:
        print(f"    Nominatim failed: {e}")
    return None, None


def try_geoapify(query):
    if not GEOAPIFY_API_KEY:
        return None, None
    try:
        resp = requests.get(
            "https://api.geoapify.com/v1/geocode/search",
            params={"text": query, "apiKey": GEOAPIFY_API_KEY, "limit": 1},
            timeout=10,
        )
        data = resp.json()
        features = data.get("features", [])
        if features:
            lon, lat = features[0]["geometry"]["coordinates"]
            return lat, lon
    except Exception as e:
        print(f"    Geoapify failed: {e}")
    return None, None


def try_google(query):
    if not GOOGLE_GEOCODING_API_KEY:
        return None, None
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": query, "key": GOOGLE_GEOCODING_API_KEY},
            timeout=10,
        )
        data = resp.json()
        results = data.get("results", [])
        if results:
            loc = results[0]["geometry"]["location"]
            return loc["lat"], loc["lng"]
    except Exception as e:
        print(f"    Google Geocoding failed: {e}")
    return None, None


def geocode_school(row, geolocator):
    """
    Tries Nominatim -> Geoapify -> Google, in that order, automatically.
    Returns (lat, lon, provider_used) or (None, None, "Not Geocoded").
    """
    query = build_query(row)
    if not query:
        return None, None, "Not Geocoded"

    lat, lon = try_nominatim(query, geolocator)
    if lat is not None:
        return lat, lon, "Nominatim"
    time.sleep(1)  # be polite to Nominatim's free tier before moving on

    lat, lon = try_geoapify(query)
    if lat is not None:
        return lat, lon, "Geoapify"

    lat, lon = try_google(query)
    if lat is not None:
        return lat, lon, "Google"

    return None, None, "Not Geocoded"


def add_coordinates(df):
    geolocator = Nominatim(user_agent="ghostwatch_hackathon")
    lats, lons, sources = [], [], []
    for _, row in df.iterrows():
        emis = row.get("EMIS_Code")
        if row.get("Data_Type") == "Synthetic":
            lats.append(None)
            lons.append(None)
            sources.append("Synthetic (no real location)")
            continue

        print(f"Geocoding {row.get('School_Name')} ({emis})...")
        lat, lon, source = geocode_school(row, geolocator)
        lats.append(lat)
        lons.append(lon)
        sources.append(source)
        print(f"  -> {source}: {lat}, {lon}")

    df["Latitude"] = lats
    df["Longitude"] = lons
    df["Geocode_Source"] = sources
    return df


# ---- STEP 2: Pull Sentinel-2 + compute Built-up Activity Index -----------
# Requires `earthengine-api` and prior `earthengine authenticate`.

def get_builtup_activity_index(lat, lon, buffer_m=60):
    """
    Returns a 0-1 score: how strongly the pixel signature around (lat, lon)
    looks like an active built structure (roof/concrete/courtyard) vs bare
    land, crop field, or vegetation regrowth (a common ghost-school pattern
    is a plot that's reverted to farmland or is simply empty).

    Uses NDBI (Normalized Difference Built-up Index) from the latest
    cloud-free Sentinel-2 composite.
    """
    import ee
    ee.Initialize()

    point = ee.Geometry.Point([lon, lat]).buffer(buffer_m)

    collection = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(point)
        .filterDate("2025-01-01", "2026-08-01")
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20))
        .sort("CLOUDY_PIXEL_PERCENTAGE")
    )
    image = collection.first()
    if image is None:
        return None

    swir1 = image.select("B11")
    nir = image.select("B8")
    ndbi = swir1.subtract(nir).divide(swir1.add(nir)).rename("NDBI")

    stats = ndbi.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=point, scale=10, maxPixels=1e6
    )
    ndbi_value = stats.get("NDBI").getInfo()
    if ndbi_value is None:
        return None

    # NDBI roughly ranges -0.5 (vegetation/water) to +0.3 (dense built-up).
    # Normalize to a 0-1 "activity" score.
    score = max(0.0, min(1.0, (ndbi_value + 0.3) / 0.6))
    return round(score, 3)


def add_satellite_scores(df):
    scores = []
    for _, row in df.iterrows():
        lat, lon = row.get("Latitude"), row.get("Longitude")
        if pd.isna(lat) or pd.isna(lon):
            scores.append(None)
            continue
        try:
            score = get_builtup_activity_index(lat, lon)
        except Exception as e:
            print(f"  satellite pull failed for {row.get('EMIS_Code')}: {e}")
            score = None
        scores.append(score)
    df["Satellite_Builtup_Activity_Score"] = scores
    return df


# ---- STEP 3: Combine with ground-truth risk score -------------------------

def combine_scores(df):
    """
    Final confidence score blends the PMIU-derived risk score (0-100, higher
    = more suspicious) with the satellite activity score (0-1, higher =
    more clearly a real built structure — so we INVERT it as a risk signal).
    Weighted 70% ground monitoring data / 30% satellite, since ground data
    is richer, but satellite catches cases where a corrupt visit report
    might not match physical reality.

    If a school has NO satellite score (geocoding failed everywhere), we
    fall back to using the ground risk score alone — the school still gets
    a final result, just without the satellite cross-check. Nothing is left
    blank or requires a person to intervene.
    """
    def blend(row):
        ground_risk = row.get("Government_Monitoring_Risk_Score")
        sat_score = row.get("Satellite_Builtup_Activity_Score")
        if ground_risk is None:
            return None
        if sat_score is None:
            return ground_risk  # no satellite signal available, use ground only
        sat_risk = (1 - sat_score) * 100  # invert: low built-up activity = high risk
        return round(0.7 * ground_risk + 0.3 * sat_risk, 2)

    df["Final_Confidence_Score"] = df.apply(blend, axis=1)
    return df


if __name__ == "__main__":
    import sys

    infile = sys.argv[1] if len(sys.argv) > 1 else "ghost_school_detector_full_dataset.csv"
    df = pd.read_csv(infile)

    print("Geocoding schools (Nominatim -> Geoapify -> Google, automatic fallback)...")
    df = add_coordinates(df)

    print("\nGeocoding summary:")
    print(df["Geocode_Source"].value_counts())

    print("\nPulling satellite indices (requires Earth Engine auth)...")
    df = add_satellite_scores(df)

    print("Blending final confidence score...")
    df = combine_scores(df)

    outfile = infile.replace(".csv", "_with_satellite.csv")
    df.to_csv(outfile, index=False)
    print(f"\nSaved: {outfile}")
    print("Every school now has a Final_Confidence_Score — no manual lookups needed.")
