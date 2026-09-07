"""
fetch_hd_satellite_images.py
==============================
Fetch high-definition aerial imagery from ArcGIS World Imagery tiles
for ALL schools (School-Level and Village-Level).

Uses Esri's World_Imagery tile endpoint (free, no API key, very reliable).
Stitches 3x3 tiles at zoom 19 (~0.29m/pixel) and crops to a 200m x 200m
window centered on each school.

For Village-Level schools (shared village-centroid coordinates), the script
deduplicates by (lat, lon) — fetching once per unique location and copying
the image to all schools at that coordinate.

OUTPUT: satellite_images/<EMIS_Code>.png  (HD aerial imagery)
"""

import math
import os
import sys
import time
import urllib.request
from io import BytesIO
from pathlib import Path

import shutil
import numpy as np
import pandas as pd
from PIL import Image

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
MERGED_CSV = SCRIPT_DIR / "ghostwatch_final_merged_v2.csv"
OUTPUT_DIR = SCRIPT_DIR / "satellite_images"

# ArcGIS World Imagery tile URL template
TILE_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)

# Parameters
ZOOM_PREFERRED = 19   # Preferred zoom level (~0.29m/pixel at 30 deg N)
ZOOM_FALLBACKS = [18, 17, 16, 15]  # Fallback zoom levels
TILE_SIZE = 256      # ArcGIS tile size in pixels
GRID = 3             # 3x3 tile grid around center tile
WINDOW_HALF_M = 100  # 100m each side = 200m total coverage
PLACEHOLDER_STD_THRESHOLD = 20  # std < this = likely "no data" tile


def latlon_to_tile(lat, lon, zoom):
    """Convert lat/lon to tile x,y at a given zoom level."""
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def tile_to_meters(tx, ty, zoom):
    """Get the top-left corner of a tile in Web Mercator meters."""
    n = 2 ** zoom
    # Tile bounds in WGS84
    lon_min = tx / n * 360.0 - 180.0
    lat_max_rad = math.atan(math.sinh(math.pi * (1 - 2 * ty / n)))
    lat_max = math.degrees(lat_max_rad)
    # Convert to meters (approximate)
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat_max))
    return lon_min * m_per_deg_lon, lat_max * m_per_deg_lat, lat_max


def fetch_tile(z, y, x):
    """Fetch a single ArcGIS tile. Returns PIL Image or None."""
    url = TILE_URL.format(z=z, y=y, x=x)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "GhostWatch/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            if len(data) > 100:
                return Image.open(BytesIO(data)).convert("RGB")
    except Exception:
        pass
    return None


def fetch_hd_image(lat, lon, zoom=19):
    """
    Fetch a 3x3 tile grid from ArcGIS World Imagery at given zoom,
    then crop to the 200m x 200m window around (lat, lon).
    Returns PIL Image or None.
    """
    # Center tile
    cx, cy = latlon_to_tile(lat, lon, zoom)

    # Get center tile's top-left corner in WGS84
    n = 2 ** zoom
    tile_lon_min = cx / n * 360.0 - 180.0
    tile_lat_max_rad = math.atan(math.sinh(math.pi * (1 - 2 * cy / n)))
    tile_lat_max = math.degrees(tile_lat_max_rad)

    # Calculate pixel offset of (lat, lon) within center tile
    tile_lat_min_rad = math.atan(math.sinh(math.pi * (1 - 2 * (cy + 1) / n)))
    tile_lat_min = math.degrees(tile_lat_min_rad)
    tile_lon_max = (cx + 1) / n * 360.0 - 180.0

    # Fractional position within the center tile (0-1)
    frac_x = (lon - tile_lon_min) / (tile_lon_max - tile_lon_min)
    frac_y = (tile_lat_max - lat) / (tile_lat_max - tile_lat_min)

    # Pixel position within center tile
    px_in_tile = frac_x * TILE_SIZE
    py_in_tile = frac_y * TILE_SIZE

    # Fetch 3x3 grid centered on (cx, cy)
    tiles = {}
    for dy in range(-1, 2):
        for dx in range(-1, 2):
            tile = fetch_tile(zoom, cy + dy, cx + dx)
            if tile is None:
                return None
            tiles[(dx, dy)] = tile
            time.sleep(0.15)  # Rate limiting

    # Stitch into a 768x768 image (3x3 tiles)
    stitched = Image.new("RGB", (TILE_SIZE * GRID, TILE_SIZE * GRID))
    for dy in range(-1, 2):
        for dx in range(-1, 2):
            sx = (dx + 1) * TILE_SIZE
            sy = (dy + 1) * TILE_SIZE
            stitched.paste(tiles[(dx, dy)], (sx, sy))

    # Calculate crop center in stitched coordinates
    center_px = TILE_SIZE + px_in_tile
    center_py = TILE_SIZE + py_in_tile

    # Calculate crop size in pixels for 200m window
    cos_lat = math.cos(math.radians(lat))
    m_per_pixel = 156543.03392 * cos_lat / (2 ** zoom)
    crop_half_px = int(WINDOW_HALF_M / m_per_pixel)

    # Crop
    left = max(0, int(center_px - crop_half_px))
    top = max(0, int(center_py - crop_half_px))
    right = min(TILE_SIZE * GRID, int(center_px + crop_half_px))
    bottom = min(TILE_SIZE * GRID, int(center_py + crop_half_px))

    cropped = stitched.crop((left, top, right, bottom))

    # Resize to standard output size
    output_size = 1024
    cropped = cropped.resize((output_size, output_size), Image.LANCZOS)

    return cropped


def is_placeholder(img):
    """Check if an image is an ArcGIS 'no data' placeholder (uniform gray)."""
    arr = np.array(img.convert("RGB"))
    return float(arr.std()) < PLACEHOLDER_STD_THRESHOLD


def fetch_hd_image_with_fallback(lat, lon):
    """Try preferred zoom, fall back to lower zooms if placeholder. Returns (img, zoom)."""
    img = fetch_hd_image(lat, lon, ZOOM_PREFERRED)
    if img is not None and not is_placeholder(img):
        return img, ZOOM_PREFERRED

    for zoom in ZOOM_FALLBACKS:
        print(f"[z{zoom}]", end=" ", flush=True)
        img = fetch_hd_image(lat, lon, zoom)
        if img is not None and not is_placeholder(img):
            return img, zoom
        time.sleep(0.3)

    return None, None


def fetch_and_save(emis, name, lat, lon, idx, total):
    """Fetch HD image for one school with zoom fallback. Returns (success, img_or_none)."""
    print(f"  [{idx}/{total}] {emis} ({name})...", end=" ", flush=True)
    try:
        img, zoom_used = fetch_hd_image_with_fallback(lat, lon)
        if img is None:
            print("FAILED (no tiles or all placeholder)")
            return False, None
        out_path = OUTPUT_DIR / f"{emis}.png"
        img.save(out_path, "PNG", optimize=True)
        kb = out_path.stat().st_size / 1024
        z_tag = f" (z{zoom_used})" if zoom_used != ZOOM_PREFERRED else ""
        print(f"OK -> {out_path.name} ({kb:.0f} KB){z_tag}")
        return True, img
    except Exception as e:
        print(f"ERROR: {e}")
        return False, None


def _needs_refetch(path):
    """Check if an existing image needs re-fetching (placeholder or too small)."""
    if path.stat().st_size < 10_000:
        return True
    try:
        img = Image.open(path)
        arr = np.array(img.convert("RGB"))
        if float(arr.std()) < PLACEHOLDER_STD_THRESHOLD:
            return True
    except Exception:
        return True
    return False


def main():
    print("=" * 65)
    print("ArcGIS World Imagery HD Tile Fetcher for GhostWatch")
    print("=" * 65)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    df = pd.read_csv(MERGED_CSV, dtype={"EMIS_Code": str})
    print(f"Total schools in CSV: {len(df)}")
    print(f"Preferred zoom: {ZOOM_PREFERRED}, fallbacks: {ZOOM_FALLBACKS}")
    print(f"Tile grid: {GRID}x{GRID} = {GRID*GRID} tiles per location")
    print(f"Coverage: 200m x 200m -> 1024x1024 output\n")

    # ---- School-Level (unique coords, one fetch per school) ----
    school_level = df[df["Satellite_Confidence"] == "School-Level"].copy()
    # Only fetch schools that don't already have an HD image
    sl_todo = []
    for _, row in school_level.iterrows():
        p = OUTPUT_DIR / f"{row['EMIS_Code']}.png"
        if not p.exists() or _needs_refetch(p):
            sl_todo.append(row)
    print(f"--- School-Level: {len(school_level)} total, {len(sl_todo)} to fetch ---")

    sl_success, sl_errors = 0, []
    for i, row in enumerate(sl_todo):
        emis = row["EMIS_Code"]
        name = row.get("School_Name", "Unknown")
        lat, lon = row["Latitude"], row["Longitude"]
        if pd.isna(lat) or pd.isna(lon):
            sl_errors.append((emis, "No coordinates"))
            continue
        ok, _ = fetch_and_save(emis, name, lat, lon, i + 1, len(sl_todo))
        if ok:
            sl_success += 1
        else:
            sl_errors.append((emis, "Fetch failed"))
        time.sleep(0.3)

    print(f"  School-Level done: {sl_success} fetched, {len(sl_errors)} errors\n")

    # ---- Village-Level (shared coords, deduplicate by location) ----
    village_level = df[df["Satellite_Confidence"] == "Village-Level"].copy()
    has_coords = village_level["Latitude"].notna() & village_level["Longitude"].notna()
    village_level = village_level[has_coords].copy()

    # Group by coordinate pair
    coord_groups = village_level.groupby(["Latitude", "Longitude"])
    unique_coords = list(coord_groups.groups.keys())

    # Filter: only coords that don't already have images for all members
    coords_todo = []
    for coord in unique_coords:
        members = coord_groups.get_group(coord)
        missing = any(
            not (OUTPUT_DIR / f"{r['EMIS_Code']}.png").exists()
            or _needs_refetch(OUTPUT_DIR / f"{r['EMIS_Code']}.png")
            for _, r in members.iterrows()
        )
        if missing:
            coords_todo.append(coord)

    print(f"--- Village-Level: {len(village_level)} schools, "
          f"{len(unique_coords)} unique coords, "
          f"{len(coords_todo)} to fetch ---")

    vl_success, vl_errors, vl_shared = 0, [], 0
    for i, coord in enumerate(coords_todo):
        lat, lon = coord
        members = coord_groups.get_group(coord)
        first_row = members.iloc[0]
        first_emis = first_row["EMIS_Code"]
        first_name = first_row.get("School_Name", "Unknown")
        n_members = len(members)

        ok, img = fetch_and_save(first_emis, first_name, lat, lon,
                                 i + 1, len(coords_todo))
        if ok and img is not None:
            vl_success += 1
            # Copy to all other schools at this coordinate
            src_path = OUTPUT_DIR / f"{first_emis}.png"
            for _, row in members.iterrows():
                if row["EMIS_Code"] != first_emis:
                    dst_path = OUTPUT_DIR / f"{row['EMIS_Code']}.png"
                    shutil.copy2(src_path, dst_path)
                    vl_shared += 1
        else:
            vl_errors.append((first_emis, f"Fetch failed ({n_members} schools at this coord)"))
        time.sleep(0.3)

    # ---- Tehsil-Level (newly geocoded, shared tehsil centroids) ----
    tehsil_level = df[df["Satellite_Confidence"] == "Tehsil-Level"].copy()
    has_coords_tl = tehsil_level["Latitude"].notna() & tehsil_level["Longitude"].notna()
    tehsil_level = tehsil_level[has_coords_tl].copy()

    if len(tehsil_level) > 0:
        coord_groups_tl = tehsil_level.groupby(["Latitude", "Longitude"])
        unique_coords_tl = list(coord_groups_tl.groups.keys())

        coords_todo_tl = []
        for coord in unique_coords_tl:
            members = coord_groups_tl.get_group(coord)
            missing = any(
                not (OUTPUT_DIR / f"{r['EMIS_Code']}.png").exists()
                or _needs_refetch(OUTPUT_DIR / f"{r['EMIS_Code']}.png")
                for _, r in members.iterrows()
            )
            if missing:
                coords_todo_tl.append(coord)

        print(f"--- Tehsil-Level: {len(tehsil_level)} schools, "
              f"{len(unique_coords_tl)} unique coords, "
              f"{len(coords_todo_tl)} to fetch ---")

        tl_success, tl_errors, tl_shared = 0, [], 0
        for i, coord in enumerate(coords_todo_tl):
            lat, lon = coord
            members = coord_groups_tl.get_group(coord)
            first_row = members.iloc[0]
            first_emis = first_row["EMIS_Code"]
            first_name = first_row.get("School_Name", "Unknown")
            n_members = len(members)

            ok, img = fetch_and_save(first_emis, first_name, lat, lon,
                                     i + 1, len(coords_todo_tl))
            if ok and img is not None:
                tl_success += 1
                src_path = OUTPUT_DIR / f"{first_emis}.png"
                for _, row in members.iterrows():
                    if row["EMIS_Code"] != first_emis:
                        dst_path = OUTPUT_DIR / f"{row['EMIS_Code']}.png"
                        shutil.copy2(src_path, dst_path)
                        tl_shared += 1
            else:
                tl_errors.append((first_emis, f"Fetch failed ({n_members} schools)"))
            time.sleep(0.3)
    else:
        tl_success, tl_errors, tl_shared = 0, [], 0
        print("--- Tehsil-Level: 0 schools ---")

    # ---- Summary ----
    print(f"\n{'=' * 65}")
    print("SUMMARY")
    print("=" * 65)
    print(f"  School-Level:   {sl_success} fetched, {len(sl_errors)} errors")
    print(f"  Village-Level:  {vl_success} unique fetched, "
          f"{vl_shared} shared copies, {len(vl_errors)} errors")
    print(f"  Tehsil-Level:   {tl_success} unique fetched, "
          f"{tl_shared} shared copies, {len(tl_errors)} errors")

    if sl_errors:
        print("\n  School-Level failures:")
        for emis, reason in sl_errors:
            print(f"    {emis}: {reason}")
    if vl_errors:
        print("\n  Village-Level failures:")
        for emis, reason in vl_errors:
            print(f"    {emis}: {reason}")
    if tl_errors:
        print("\n  Tehsil-Level failures:")
        for emis, reason in tl_errors:
            print(f"    {emis}: {reason}")

    total = len([f for f in os.listdir(OUTPUT_DIR) if f.endswith(".png")])
    print(f"\n  Total images in {OUTPUT_DIR}: {total}")
    print("Done.")


if __name__ == "__main__":
    main()
