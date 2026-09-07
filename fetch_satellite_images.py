"""
fetch_satellite_images.py
=========================
Fetch Sentinel-2 RGB satellite images for School-Level confidence schools.

Uses Microsoft Planetary Computer's sentinel-2-l2a collection to download
a 200m x 200m true-color crop around each school's coordinates.

Only processes schools with Satellite_Confidence == "School-Level"
(unique coordinates — not village-centroid duplicates).

OUTPUT: satellite_images/<EMIS_Code>.png  (one image per school)

REQUIRES: pip install planetary-computer pystac-client rasterio numpy pandas Pillow
"""

import os
import sys
import time
import numpy as np
import pandas as pd

# Force UTF-8 on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import planetary_computer
    import pystac_client
    import rasterio
    from rasterio.warp import transform as warp_transform
    from rasterio.windows import from_bounds
    from PIL import Image
except ImportError as e:
    print(f"ERROR: Missing dependency: {e}")
    print("Please run: pip install planetary-computer pystac-client rasterio numpy Pillow")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MERGED_CSV = os.path.join(SCRIPT_DIR, "ghostwatch_final_merged_v2.csv")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "satellite_images")

STAC_API_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-2-l2a"
BANDS = ["B04", "B03", "B02"]  # Red, Green, Blue (10m resolution each)

# 200m x 200m window around the school
WINDOW_HALF_M = 100  # 100m each side of center
OUTPUT_SIZE = 512     # Resize to 512x512 for dashboard display

# Search window: look back 18 months for clear imagery
MONTHS_BACK = 18
MAX_CLOUD_COVER = 25  # percent


def search_sentinel2(lat, lon, start_date, end_date, max_cloud=MAX_CLOUD_COVER):
    """Search Planetary Computer for clear Sentinel-2 scenes."""
    offset = 0.02  # ~2km search bbox
    bbox = [lon - offset, lat - offset, lon + offset, lat + offset]

    catalog = pystac_client.Client.open(STAC_API_URL)
    search = catalog.search(
        collections=[COLLECTION],
        datetime=f"{start_date}/{end_date}",
        bbox=bbox,
        query={"eo:cloud_cover": {"lt": max_cloud}},
        limit=20,
        sortby=[{"field": "datetime", "direction": "desc"}],
    )
    items = list(search.items())
    if not items:
        return []
    return [planetary_computer.sign(item) for item in items]


def fetch_rgb_image(lat, lon, start_date, end_date):
    """
    Fetch a true-color RGB image from Sentinel-2 around (lat, lon).

    Returns a PIL Image or None on failure.
    Tries multiple STAC items to handle UTM zone boundary issues.
    """
    items = search_sentinel2(lat, lon, start_date, end_date)
    if not items:
        return None

    bands_data = {}
    for item in items:
        bands_data = {}
        ok = True
        for band_name in BANDS:
            asset = item.assets.get(band_name)
            if not asset:
                ok = False
                break
            href = asset.href
            try:
                with rasterio.open(href) as src:
                    # Transform WGS84 point to raster CRS (always UTM-projected)
                    xs, ys = warp_transform("EPSG:4326", src.crs, [lon], [lat])
                    x_m, y_m = xs[0], ys[0]

                    left = x_m - WINDOW_HALF_M
                    bottom = y_m - WINDOW_HALF_M
                    right = x_m + WINDOW_HALF_M
                    top = y_m + WINDOW_HALF_M

                    window = from_bounds(left, bottom, right, top, src.transform)
                    data = src.read(1, window=window, boundless=True, fill_value=0)

                    # Skip if all zeros (tile edge / no coverage)
                    if np.all(data == 0):
                        ok = False
                        break

                    bands_data[band_name] = data.astype(np.float32)
            except Exception as e:
                ok = False
                break

        if ok and len(bands_data) == 3:
            # Successfully read all 3 bands from this item
            break
        bands_data = {}

    if len(bands_data) != 3:
        return None

    # Stack into RGB array (H, W, 3)
    r = bands_data["B04"]
    g = bands_data["B03"]
    b = bands_data["B02"]
    rgb = np.stack([r, g, b], axis=-1)

    # Normalize: percentile stretch for good visual contrast
    # Clip negatives / saturated values, then 2-98 percentile stretch
    rgb = np.clip(rgb, 0, 10000)
    for i in range(3):
        ch = rgb[:, :, i]
        p2 = np.percentile(ch[ch > 0], 2) if np.any(ch > 0) else 0
        p98 = np.percentile(ch[ch > 0], 98) if np.any(ch > 0) else 1
        if p98 > p2:
            rgb[:, :, i] = np.clip((ch - p2) / (p98 - p2), 0, 1)
        else:
            rgb[:, :, i] = 0

    # Convert to uint8
    rgb_uint8 = (rgb * 255).astype(np.uint8)
    img = Image.fromarray(rgb_uint8, "RGB")
    img = img.resize((OUTPUT_SIZE, OUTPUT_SIZE), Image.LANCZOS)
    return img


def main():
    print("=" * 70)
    print("Sentinel-2 RGB Image Fetcher for GhostWatch")
    print("=" * 70)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    df = pd.read_csv(MERGED_CSV, dtype={"EMIS_Code": str})
    print(f"Loaded {len(df)} schools from CSV")

    # Filter: School-Level confidence only
    school_level = df[df["Satellite_Confidence"] == "School-Level"].copy()
    print(f"School-Level schools to process: {len(school_level)}")

    # Skip schools that already have an image (resume support)
    existing = set()
    for f in os.listdir(OUTPUT_DIR):
        if f.endswith(".png"):
            existing.add(f.replace(".png", ""))

    todo = school_level[~school_level["EMIS_Code"].isin(existing)]
    print(f"Already have images: {len(existing & set(school_level['EMIS_Code']))}")
    print(f"To fetch: {len(todo)}\n")

    from datetime import datetime, timedelta
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=MONTHS_BACK * 30)).strftime("%Y-%m-%d")
    print(f"Date range: {start_date} to {end_date}")
    print(f"Max cloud cover: {MAX_CLOUD_COVER}%\n")

    success, skipped, errors = [], [], []

    for i, (_, row) in enumerate(todo.iterrows()):
        emis = row["EMIS_Code"]
        name = row.get("School_Name", "Unknown")
        lat, lon = row["Latitude"], row["Longitude"]

        if pd.isna(lat) or pd.isna(lon):
            skipped.append((emis, "No coordinates"))
            print(f"  [{i+1}/{len(todo)}] {emis} ({name}): SKIP — no coordinates")
            continue

        print(f"  [{i+1}/{len(todo)}] {emis} ({name}) @ ({lat:.4f}, {lon:.4f})...", end=" ", flush=True)

        try:
            img = fetch_rgb_image(lat, lon, start_date, end_date)
            if img is None:
                skipped.append((emis, "No clear imagery found"))
                print("SKIP — no clear imagery")
            else:
                out_path = os.path.join(OUTPUT_DIR, f"{emis}.png")
                img.save(out_path, "PNG")
                success.append(emis)
                print(f"OK -> {os.path.basename(out_path)}")
        except Exception as e:
            errors.append((emis, str(e)))
            print(f"ERROR — {e}")

        time.sleep(1)  # Rate limiting

    # Summary
    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print("=" * 70)
    print(f"  School-Level schools total : {len(school_level)}")
    print(f"  Already had images          : {len(existing & set(school_level['EMIS_Code']))}")
    print(f"  Successfully fetched       : {len(success)}")
    print(f"  Skipped (no data)          : {len(skipped)}")
    print(f"  Errors                     : {len(errors)}")

    if skipped:
        print(f"\nSkipped schools:")
        for emis, reason in skipped:
            print(f"  {emis}: {reason}")

    if errors:
        print(f"\nErrors:")
        for emis, err in errors:
            print(f"  {emis}: {err}")

    total_images = len([f for f in os.listdir(OUTPUT_DIR) if f.endswith(".png")])
    print(f"\nTotal images in {OUTPUT_DIR}: {total_images}")
    print("Done.")


if __name__ == "__main__":
    main()
