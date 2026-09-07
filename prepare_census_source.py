"""
prepare_census_source.py
========================
One-time preparation of the Punjab Annual School Census 2017-18 source file
for the local census viewer.

Why
---
The census rows in ghostwatch_final_merged_v2.csv carry a generic Open Data
Pakistan dataset URL (no per-school view exists on that site), so the
dashboard ships the ACTUAL government census spreadsheet and serves its own
viewer: a searchable table over the real file with the clicked school's row
scrolled-to and highlighted (matched by EMIS_Code).

What it does
------------
1. Reads census_source/public-census_oct_2018.xlsx (the original XLSX
   downloaded from Open Data Pakistan -- see provenance.json).
2. Writes census_source/census_public_2018.csv: every row (52,470 public
   schools) and every column of the original 'school' sheet, in the original
   order, values as plain text.  This CSV is what the backend loads (fast);
   the XLSX itself is kept untouched as the archival source.
3. Writes census_source/provenance.json: file name, source URLs, size,
   SHA-256, download date, row/column counts -- transparency for the viewer.
4. Verifies every Real (Census) EMIS_Code in ghostwatch_final_merged_v2.csv
   exists in the census file (they all do; the check guards future data
   refreshes).

Run:  python prepare_census_source.py        (only needed after re-downloading)
"""
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
XLSX = ROOT / "census_source" / "public-census_oct_2018.xlsx"
OUT_CSV = ROOT / "census_source" / "census_public_2018.csv"
PROVENANCE = ROOT / "census_source" / "provenance.json"
FINAL_CSV = ROOT / "ghostwatch_final_merged_v2.csv"

DATASET_URL = "https://opendata.com.pk/dataset/punjab-annual-school-census-report-2017-18"
RESOURCE_URL = ("https://opendata.com.pk/dataset/3560f1d7-b9ab-4a1d-b0e9-150ae853dd12/"
                "resource/7baf2669-333c-429a-ba2a-d000ded4ae8f/download/"
                "public-census_oct_2018.xlsx")
DOWNLOADED = "2026-09-06"  # date the XLSX was fetched into this project


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    if not XLSX.exists():
        sys.exit(f"ERROR: {XLSX} not found -- download it from {RESOURCE_URL}")

    print(f"Reading {XLSX.name} (this parses a 52k-row workbook, takes a moment)...")
    df = pd.read_excel(XLSX, dtype=str, engine="openpyxl")
    print(f"  sheet 'school': {len(df)} rows x {len(df.columns)} columns")

    # Preserve the original column order and row order; write values as text.
    df.to_csv(OUT_CSV, index=False)
    print(f"Wrote {OUT_CSV.name}  ({len(df)} rows, {len(df.columns)} cols, "
          f"{OUT_CSV.stat().st_size:,} bytes)")

    # --- Verify every census school in our dataset exists in the file --------
    emis = df["emiscode"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    ours = pd.read_csv(FINAL_CSV, dtype={"EMIS_Code": str})
    census_rows = ours[ours["Data_Type"] == "Real (Census)"]
    our_codes = census_rows["EMIS_Code"].astype(str).str.strip()
    missing = our_codes[~our_codes.isin(set(emis))]
    print(f"\nVerification: {len(our_codes) - len(missing)}/{len(our_codes)} "
          f"census schools found in the census file by emiscode")
    if len(missing):
        print("  MISSING:", missing.tolist()[:20])
        sys.exit("ERROR: census file does not cover all census rows -- aborting")

    # --- Provenance sidecar --------------------------------------------------
    prov = {
        "title": "Punjab Annual School Census Report 2017-18 (public schools)",
        "file": XLSX.name,
        "sheet": "school",
        "rows": int(len(df)),
        "columns": int(len(df.columns)),
        "size_bytes": XLSX.stat().st_size,
        "sha256": sha256_of(XLSX),
        "downloaded": DOWNLOADED,
        "dataset_url": DATASET_URL,
        "resource_url": RESOURCE_URL,
        "publisher": "PMIU / PESRP via Open Data Pakistan (NCBC)",
        "note": ("Original government census spreadsheet, stored unmodified. "
                 "The site offers no per-school view, only full-file download; "
                 "GhostWatch's local viewer highlights each school's own row."),
    }
    PROVENANCE.write_text(json.dumps(prov, indent=2), encoding="utf-8")
    print(f"Wrote {PROVENANCE.name}")

    print("\nDone. The backend now serves /census/* endpoints from "
          f"{OUT_CSV.name}.")


if __name__ == "__main__":
    main()
