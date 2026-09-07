"""
Ghost School Detector — FastAPI Backend
========================================
Loads ghostwatch_final_merged_v2.csv (the single source of truth produced by
merge_satellite_scores_v2.py) and exposes four endpoints for a frontend.

All risk scoring is done OFFLINE in the merge pipeline — this backend
reads pre-computed columns (Ghost_Risk_Score_Tier1 / Infrastructure_
Screening_Score and Priority) and does NOT recompute anything.

Endpoints
---------
GET /schools          List schools with pagination, filtering, and search
GET /schools/{emis}   Full detail for one school
GET /schools/{emis}/raw   Every column/value of the school's row in the
                          source CSV, exactly as stored (raw source data)
GET /census/meta      Provenance of the bundled Punjab census source file
GET /census/locate/{emis}  Row index of one school inside the census file
GET /census/rows      Census rows in original file order (windowed)
GET /census/search    Search the census file by emiscode / school name
GET /districts        Sorted list of unique district names (filter dropdown)
GET /flagged          Schools sorted by risk_score desc
GET /stats            Aggregate counts by district & audit_priority

Census source file
------------------
The 251 Real (Census) rows originate from the Punjab Annual School Census
2017-18 (public schools) published on Open Data Pakistan.  That site offers
no per-school view, so the project bundles the original government XLSX
(census_source/, see provenance.json) and this backend serves a local viewer
over it: /census/* endpoints expose the real file so the dashboard can show
the clicked school's own census row, highlighted and searchable.  Run
prepare_census_source.py (project root) if the CSV is missing.

Run
---
    pip install -r requirements.txt
    uvicorn app:app --reload
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

# =========================================================================
# Data loading
# =========================================================================

DATA_PATH: Path = Path(__file__).resolve().parent.parent / "ghostwatch_final_merged_v2.csv"
SATELLITE_IMG_DIR: Path = Path(__file__).resolve().parent.parent / "satellite_images"
CENSUS_DIR: Path = Path(__file__).resolve().parent.parent / "census_source"
CENSUS_CSV: Path = CENSUS_DIR / "census_public_2018.csv"
CENSUS_PROVENANCE: Path = CENSUS_DIR / "provenance.json"


def _safe_float(v: Any) -> Optional[float]:
    """Return None for NaN / non-numeric values."""
    if v is None:
        return None
    if isinstance(v, float):
        return None if pd.isna(v) else v
    return v


def _safe_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    s = str(v).strip()
    return s if s and s.lower() != "nan" else None


def _safe_int(v: Any) -> Optional[int]:
    """Return None for NaN / non-numeric values, else int."""
    if v is None:
        return None
    if isinstance(v, float):
        return None if pd.isna(v) else int(v)
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def load_dataset() -> pd.DataFrame:
    """Read the merged CSV and alias the pre-computed columns.

    risk_score      : Ghost_Risk_Score_Tier1 for Tier 1 rows, falling back
                      to Infrastructure_Screening_Score for Tier 2 rows.
    audit_priority  : Priority column (already computed by the pipeline).
    """
    df = pd.read_csv(DATA_PATH, dtype={"EMIS_Code": str})

    # Build a unified risk_score from whichever tier column is populated.
    # Kept at the CSV's own 2-decimal precision (NOT int-truncated) so the
    # dashboard shows exactly what the CSV and map show (e.g. 82.5, not 82).
    tier1_score = pd.to_numeric(df["Ghost_Risk_Score_Tier1"], errors="coerce")
    tier2_score = pd.to_numeric(df["Infrastructure_Screening_Score"], errors="coerce")
    df["risk_score"] = tier1_score.fillna(tier2_score).fillna(0).round(2)

    # Alias the pre-computed Priority column
    df["audit_priority"] = df["Priority"].fillna("Low")

    return df


# =========================================================================
# Pydantic models
# =========================================================================


class SchoolSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    EMIS_Code: str
    School_Name: Optional[str] = None
    District: Optional[str] = None
    Tehsil: Optional[str] = None
    Data_Type: Optional[str] = None
    School_Status: Optional[str] = None
    Latitude: Optional[float] = None
    Longitude: Optional[float] = None
    risk_score: float = 0
    audit_priority: str = "Low"
    Satellite_Density_Flag: Optional[str] = None
    Satellite_Confidence: Optional[str] = None
    Qwen_Fraud_Flag: Optional[str] = None
    Final_GhostSchool_Reason: Optional[str] = None
    Data_Risk_Reason: Optional[str] = None
    Satellite_Risk_Reason: Optional[str] = None
    Qwen_Fraud_Reason: Optional[str] = None


class SchoolDetail(SchoolSummary):
    # -- infrastructure fields --
    Level: Optional[str] = None
    Building_Illegal_Occupation: Optional[str] = None
    Teachers_Sanctioned: Optional[float] = None
    Teachers_Filled: Optional[float] = None
    Teachers_Present_Today: Optional[float] = None
    Teachers_Absent_Today: Optional[float] = None
    Total_Enrolled: Optional[float] = None
    Total_Present: Optional[float] = None
    Toilets_Total: Optional[int] = None
    Toilets_Functional: Optional[int] = None
    Boundary_Wall: Optional[str] = None
    Electricity: Optional[str] = None
    Drinking_Water: Optional[str] = None
    Cleanliness_Building: Optional[str] = None
    Notes: Optional[str] = None
    Monitoring_Date: Optional[str] = None
    Source_URL: Optional[str] = None
    # -- scoring / risk breakdown --
    Ghost_Risk_Score_Tier1: Optional[float] = None
    Infrastructure_Screening_Score: Optional[float] = None
    Data_Risk_Reason: Optional[str] = None
    Qwen_Fraud_Reason: Optional[str] = None
    Final_GhostSchool_Reason: Optional[str] = None
    Tier: Optional[str] = None
    Confidence_Level: Optional[str] = None
    # -- satellite analysis --
    Satellite_BuiltUp_Percent: Optional[float] = None
    Percent_Built_Earliest: Optional[float] = None
    Percent_Built_Recent: Optional[float] = None
    Satellite_Risk_Reason: Optional[str] = None
    has_satellite_image: bool = False
    # -- landcover raw --
    Landcover_Year1: Optional[float] = None
    Landcover_Class1: Optional[str] = None
    Landcover_Year2: Optional[float] = None
    Landcover_Class2: Optional[str] = None


class SchoolListResponse(BaseModel):
    total: int
    skip: int
    limit: int
    items: List[SchoolSummary]


class StatsResponse(BaseModel):
    total_schools: int
    by_priority: Dict[str, int]
    by_district: Dict[str, int]
    avg_risk_score: float
    avg_risk_by_priority: Dict[str, float]
    avg_risk_by_district: Dict[str, float]


# =========================================================================
# App
# =========================================================================

app = FastAPI(
    title="Ghost School Detector API",
    description="Risk-scored school data for audit prioritisation",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Loaded once at startup
_df: pd.DataFrame = load_dataset()

# Second view of the same CSV with EVERY column read as plain text: the
# /schools/{emis}/raw endpoint returns each cell exactly as stored in the
# file (numbers keep their textual form, empty cells become null) so the
# dashboard can show the exact underlying data behind the score.
_raw_df: pd.DataFrame = pd.read_csv(DATA_PATH, dtype=str)


# Bundled Punjab Annual School Census 2017-18 (public schools): the REAL
# government file behind the 251 Real (Census) rows, served so the dashboard
# can open a searchable local viewer with each school's own row highlighted.
# Loaded best-effort: if the CSV is missing the /census/* endpoints return a
# clear error instead of crashing the whole API.
_census_df: Optional[pd.DataFrame] = None
_census_meta: Dict[str, Any] = {}


def _load_census() -> Optional[pd.DataFrame]:
    if not CENSUS_CSV.exists():
        print(f"[census] {CENSUS_CSV.name} not found -- /census/* disabled "
              "(run prepare_census_source.py)")
        return None
    df = pd.read_csv(CENSUS_CSV, dtype=str)
    # Memory trim: most census columns are low-cardinality (districts, yes/no
    # flags, ...).  Categorising them shares one copy per distinct value with
    # no change to the string values the endpoints return.
    for col in df.columns:
        if col in ("emiscode", "school_name"):
            continue  # keep the two search columns as plain strings
        if df[col].nunique(dropna=False) < 1000:
            df[col] = df[col].astype("category")
    print(f"[census] loaded {len(df)} rows x {len(df.columns)} cols from "
          f"{CENSUS_CSV.name}")
    return df


_census_df = _load_census()
if CENSUS_PROVENANCE.exists():
    _census_meta = json.loads(CENSUS_PROVENANCE.read_text(encoding="utf-8"))


def _require_census() -> pd.DataFrame:
    """Census dataframe or a 503 explaining how to prepare it."""
    if _census_df is None:
        raise HTTPException(
            status_code=503,
            detail=("Census source file not prepared. Run "
                    "'python prepare_census_source.py' in the project root."),
        )
    return _census_df


def _census_row_to_dict(index: int, row: pd.Series) -> Dict[str, Any]:
    """One census row -> dict with its 0-based file index + all columns as text."""
    out: Dict[str, Any] = {"index": int(index)}
    for col in _census_df.columns:  # type: ignore[union-attr]
        v = row.get(col)
        out[col] = None if pd.isna(v) else str(v)
    return out


def _row_to_summary(row: pd.Series) -> dict:
    return {
        "EMIS_Code": str(row["EMIS_Code"]),
        "School_Name": _safe_str(row.get("School_Name")),
        "District": _safe_str(row.get("District")),
        "Tehsil": _safe_str(row.get("Tehsil")),
        "Data_Type": _safe_str(row.get("Data_Type")),
        "School_Status": _safe_str(row.get("School_Status")),
        "Latitude": _safe_float(row.get("Latitude")),
        "Longitude": _safe_float(row.get("Longitude")),
        "risk_score": round(float(row["risk_score"]), 2),
        "audit_priority": str(row["audit_priority"]),
        "Satellite_Density_Flag": _safe_str(row.get("Satellite_Density_Flag")),
        "Satellite_Confidence": _safe_str(row.get("Satellite_Confidence")),
        "Qwen_Fraud_Flag": _safe_str(row.get("Qwen_Fraud_Flag")),
        "Final_GhostSchool_Reason": _safe_str(row.get("Final_GhostSchool_Reason")),
        "Data_Risk_Reason": _safe_str(row.get("Data_Risk_Reason")),
        "Satellite_Risk_Reason": _safe_str(row.get("Satellite_Risk_Reason")),
        "Qwen_Fraud_Reason": _safe_str(row.get("Qwen_Fraud_Reason")),
    }


def _row_to_detail(row: pd.Series) -> dict:
    d = _row_to_summary(row)
    d.update(
        {
            # infrastructure
            "Level": _safe_str(row.get("Level")),
            "Building_Illegal_Occupation": _safe_str(row.get("Building_Illegal_Occupation")),
            "Teachers_Sanctioned": _safe_float(row.get("Teachers_Sanctioned")),
            "Teachers_Filled": _safe_float(row.get("Teachers_Filled")),
            "Teachers_Present_Today": _safe_float(row.get("Teachers_Present_Today")),
            "Teachers_Absent_Today": _safe_float(row.get("Teachers_Absent_Today")),
            "Total_Enrolled": _safe_float(row.get("Total_Enrolled")),
            "Total_Present": _safe_float(row.get("Total_Present")),
            "Toilets_Total": int(row["Toilets_Total"]) if pd.notna(row.get("Toilets_Total")) else None,
            "Toilets_Functional": int(row["Toilets_Functional"]) if pd.notna(row.get("Toilets_Functional")) else None,
            "Boundary_Wall": _safe_str(row.get("Boundary_Wall")),
            "Electricity": _safe_str(row.get("Electricity")),
            "Drinking_Water": _safe_str(row.get("Drinking_Water")),
            "Cleanliness_Building": _safe_str(row.get("Cleanliness_Building")),
            "Notes": _safe_str(row.get("Notes")),
            "Monitoring_Date": _safe_str(row.get("Monitoring_Date")),
            "Source_URL": _safe_str(row.get("Source_URL")),
            # scoring / risk breakdown
            "Ghost_Risk_Score_Tier1": _safe_float(row.get("Ghost_Risk_Score_Tier1")),
            "Infrastructure_Screening_Score": _safe_float(row.get("Infrastructure_Screening_Score")),
            "Data_Risk_Reason": _safe_str(row.get("Data_Risk_Reason")),
            "Qwen_Fraud_Reason": _safe_str(row.get("Qwen_Fraud_Reason")),
            "Final_GhostSchool_Reason": _safe_str(row.get("Final_GhostSchool_Reason")),
            "Tier": _safe_str(row.get("Tier")),
            "Confidence_Level": _safe_str(row.get("Confidence_Level")),
            # satellite analysis
            "Satellite_BuiltUp_Percent": _safe_float(row.get("Satellite_BuiltUp_Percent")),
            "Percent_Built_Earliest": _safe_float(row.get("Percent_Built_Earliest")),
            "Percent_Built_Recent": _safe_float(row.get("Percent_Built_Recent")),
            "Satellite_Risk_Reason": _safe_str(row.get("Satellite_Risk_Reason")),
            "has_satellite_image": (SATELLITE_IMG_DIR / f"{row['EMIS_Code']}.png").exists(),
            # landcover raw
            "Landcover_Year1": _safe_float(row.get("Landcover_Year1")),
            "Landcover_Class1": _safe_str(row.get("Landcover_Class1")),
            "Landcover_Year2": _safe_float(row.get("Landcover_Year2")),
            "Landcover_Class2": _safe_str(row.get("Landcover_Class2")),
        }
    )
    return d


# ---- GET /schools ----------------------------------------------------

@app.get("/schools", response_model=SchoolListResponse, tags=["schools"])
def list_schools(
    district: Optional[str] = Query(None, description="Filter by district (case-insensitive)"),
    priority: Optional[str] = Query(None, description="Filter by audit_priority: High, Medium, Low"),
    status: Optional[str] = Query(None, description="Filter by School_Status (case-insensitive)"),
    fraud_flag: Optional[str] = Query(None, description="Filter by Qwen_Fraud_Flag: True or False"),
    satellite: Optional[str] = Query(None, description="Filter by satellite RISK level: High, Medium, Low, NotVerified (or raw flags Low_Density/Mixed_Density/High_Density/NoData)"),
    sort: Optional[str] = Query(None, description="Sort modes, comma-separated: 'flagged_first' (fraud-flagged first), 'high_first' (high satellite risk first); risk score desc breaks ties"),
    search: Optional[str] = Query(None, description="Search by school name or EMIS code (case-insensitive)"),
    skip: int = Query(0, ge=0, description="Pagination offset"),
    limit: int = Query(50, ge=1, le=300, description="Page size"),
):
    """List schools with pagination, filtering, search, and optional sorting."""
    df = _df

    if district:
        df = df[df["District"].str.lower() == district.lower()]

    if priority:
        df = df[df["audit_priority"].str.lower() == priority.lower()]

    if status:
        df = df[df["School_Status"].str.lower() == status.lower()]

    if fraud_flag:
        want_true = fraud_flag.strip().lower() in ("true", "flagged", "yes", "1")
        df = df[df["Qwen_Fraud_Flag"].astype(str).str.strip().str.lower() == ("true" if want_true else "false")]

    if satellite:
        # Satellite RISK is the inverse of the density flag: Low_Density around a
        # school = HIGH ghost risk (almost nothing built there), High_Density = low risk.
        key = satellite.strip().lower()
        risk_to_flag = {"high": "Low_Density", "medium": "Mixed_Density", "low": "High_Density"}
        if key in ("notverified", "not_verified", "nodata"):
            df = df[
                df["Satellite_Density_Flag"].isna()
                | (df["Satellite_Density_Flag"].astype(str).str.strip() == "NoData")
            ]
        elif key in risk_to_flag:
            df = df[df["Satellite_Density_Flag"].astype(str).str.strip() == risk_to_flag[key]]
        elif key in ("low_density", "mixed_density", "high_density"):
            df = df[df["Satellite_Density_Flag"].astype(str).str.strip() == key.title()]

    if search:
        q = search.lower()
        # regex=False: treat the search string literally so characters like
        # '(' or '[' typed into the search box don't raise a regex error (HTTP 500)
        mask = (
            df["School_Name"].str.lower().str.contains(q, na=False, regex=False)
            | df["EMIS_Code"].str.lower().str.contains(q, na=False, regex=False)
        )
        df = df[mask]

    # Optional ordering applied BEFORE pagination so flagged / high-satellite-risk
    # schools lead page 1. Modes may be combined: sort=flagged_first,high_first
    sorts = [s.strip().lower() for s in (sort or "").split(",") if s.strip()]
    if "flagged_first" in sorts or "high_first" in sorts:
        df = df.copy()
        keys, orders = [], []
        if "flagged_first" in sorts:
            df["_flagged"] = (
                df["Qwen_Fraud_Flag"].astype(str).str.strip().str.lower() == "true"
            ).astype(int)
            keys.append("_flagged")
            orders.append(False)
        if "high_first" in sorts:
            # Severity: Low_Density=High(3), Mixed_Density=Medium(2), High_Density=Low(1), NoData/missing=0
            df["_satrank"] = (
                df["Satellite_Density_Flag"].astype(str).str.strip()
                .map({"Low_Density": 3, "Mixed_Density": 2, "High_Density": 1})
                .fillna(0)
            )
            keys.append("_satrank")
            orders.append(False)
        keys.append("risk_score")
        orders.append(False)
        df = df.sort_values(keys, ascending=orders)
    else:
        # Default: highest risk first — keeps pagination consistent with the
        # dashboard's default "Risk Score desc" sort state (the CSV itself is
        # not score-ordered, so without this the top scores could sit on any page)
        df = df.sort_values("risk_score", ascending=False)

    total = len(df)
    page = df.iloc[skip : skip + limit]
    return SchoolListResponse(
        total=total,
        skip=skip,
        limit=limit,
        items=[_row_to_summary(row) for _, row in page.iterrows()],
    )


# ---- GET /schools/{emis_code} ----------------------------------------

@app.get("/schools/{emis_code}", response_model=SchoolDetail, tags=["schools"])
def get_school(emis_code: str):
    """Return full detail for a single school by EMIS code."""
    matches = _df[_df["EMIS_Code"] == emis_code]
    if matches.empty:
        raise HTTPException(status_code=404, detail=f"School {emis_code} not found")
    return _row_to_detail(matches.iloc[0])


# ---- GET /schools/{emis_code}/raw ------------------------------------

@app.get("/schools/{emis_code}/raw", response_model=Dict[str, Any], tags=["schools"])
def get_school_raw(emis_code: str):
    """Every column and value from this school's row in the source CSV.

    Returns all 40 columns of ghostwatch_final_merged_v2.csv in file order,
    with each cell EXACTLY as stored (values as text, empty cells as null) —
    the underlying data that produced the risk score, satellite flag and
    reason.  No transformation, no derived fields.
    """
    matches = _raw_df[_raw_df["EMIS_Code"] == emis_code]
    if matches.empty:
        raise HTTPException(status_code=404, detail=f"School {emis_code} not found")
    row = matches.iloc[0]
    return {col: (None if pd.isna(row[col]) else str(row[col]))
            for col in _raw_df.columns}


# ---- GET /census/meta -------------------------------------------------

@app.get("/census/meta", response_model=Dict[str, Any], tags=["census"])
def census_meta():
    """Provenance of the bundled Punjab Annual School Census source file.

    The original government XLSX (downloaded from Open Data Pakistan) ships
    with the project in census_source/; this endpoint reports its origin,
    checksum and shape so the local viewer can label itself honestly.
    """
    df = _require_census()
    return {
        **_census_meta,
        "loaded_rows": len(df),
        "loaded_columns": list(df.columns),
    }


# ---- GET /census/locate/{emis_code} ------------------------------------

@app.get("/census/locate/{emis_code}", response_model=Dict[str, Any], tags=["census"])
def census_locate(emis_code: str):
    """Locate one school's row inside the census file (matched by emiscode).

    Returns the 0-based row index in original file order plus the school
    name as recorded in the census -- the viewer uses this to open a window
    of neighbouring rows scrolled to and highlighting this school.
    """
    df = _require_census()
    codes = df["emiscode"].astype(str).str.strip()
    hits = df.index[codes == emis_code.strip()]
    if len(hits) == 0:
        raise HTTPException(
            status_code=404,
            detail=f"EMIS {emis_code} not found in the census source file",
        )
    i = int(hits[0])
    row = df.iloc[i]
    return {
        "found": True,
        "index": i,
        "emiscode": str(row["emiscode"]),
        "school_name": _safe_str(row.get("school_name")),
    }


# ---- GET /census/rows --------------------------------------------------

@app.get("/census/rows", response_model=Dict[str, Any], tags=["census"])
def census_rows(
    start: int = Query(0, ge=0, description="First row index (0-based, file order)"),
    count: int = Query(60, ge=1, le=200, description="How many rows to return"),
):
    """Census rows in original file order (windowed for the local viewer)."""
    df = _require_census()
    start = min(start, max(len(df) - 1, 0))
    page = df.iloc[start : start + count]
    return {
        "total": len(df),
        "start": start,
        "count": len(page),
        "rows": [_census_row_to_dict(start + k, row)
                 for k, (_, row) in enumerate(page.iterrows())],
    }


# ---- GET /census/search ------------------------------------------------

@app.get("/census/search", response_model=Dict[str, Any], tags=["census"])
def census_search(
    q: str = Query(..., min_length=1, max_length=100,
                   description="Search emiscode prefix or school-name substring"),
    limit: int = Query(100, ge=1, le=200, description="Max rows to return"),
):
    """Search the real census file by emiscode prefix or school-name substring.

    Case-insensitive; literal matching (regex off) so punctuation in the
    query never breaks the search.  Returns matches in file order with their
    row indices, plus the total number of matches.
    """
    df = _require_census()
    needle = q.strip().lower()
    if not needle:
        return {"total_matches": 0, "limit": limit, "rows": []}
    codes = df["emiscode"].astype(str).str.strip().str.lower()
    names = df["school_name"].astype(str).str.lower()
    # regex=False: treat the query literally (same guard as /schools search)
    mask = codes.str.startswith(needle, na=False) | names.str.contains(
        needle, na=False, regex=False)
    hits = df.index[mask]
    page = df.loc[hits[:limit]]
    return {
        "total_matches": int(len(hits)),
        "limit": limit,
        "rows": [_census_row_to_dict(int(idx), row)
                 for idx, (_, row) in zip(hits[:limit], page.iterrows())],
    }


# ---- GET /districts ----------------------------------------------------

@app.get("/districts", response_model=List[str], tags=["filters"])
def list_districts():
    """Sorted list of unique district names (for filter dropdowns)."""
    return sorted(
        _df["District"].dropna().astype(str).str.strip().unique().tolist()
    )


# ---- GET /flagged ----------------------------------------------------

@app.get("/flagged", response_model=List[SchoolSummary], tags=["flagged"])
def flagged_schools(
    limit: int = Query(50, ge=1, le=300, description="Max schools to return"),
):
    """Top schools by risk score, highest first.

    Note: despite the endpoint name this is a RISK ranking, not a list of
    fraud-flagged schools.  For Qwen fraud flags use
    /schools?fraud_flag=True instead."""
    df = _df.sort_values("risk_score", ascending=False).head(limit)
    return [_row_to_summary(row) for _, row in df.iterrows()]


# ---- GET /stats ------------------------------------------------------

@app.get("/stats", response_model=StatsResponse, tags=["stats"])
def stats():
    """Aggregate counts by district and audit priority, average risk score."""
    priority_counts = _df["audit_priority"].value_counts().to_dict()
    district_counts = _df["District"].value_counts().to_dict()

    avg_by_priority = (
        _df.groupby("audit_priority")["risk_score"]
        .mean()
        .round(1)
        .to_dict()
    )
    avg_by_district = (
        _df.groupby("District")["risk_score"]
        .mean()
        .round(1)
        .to_dict()
    )

    return StatsResponse(
        total_schools=len(_df),
        by_priority=priority_counts,
        by_district=district_counts,
        avg_risk_score=round(float(_df["risk_score"].mean()), 1),
        avg_risk_by_priority=avg_by_priority,
        avg_risk_by_district=avg_by_district,
    )


# ---- GET /satellite-image/{emis_code} --------------------------------

@app.get("/satellite-image/{emis_code}", tags=["satellite"])
def satellite_image(emis_code: str):
    """Return the HD aerial reference image PNG for a school.

    Images are stitched ArcGIS World Imagery tiles (~0.3-0.6 m/pixel,
    200x200 m window around the school).  No algorithmic enhancement
    is applied."""
    img_path = SATELLITE_IMG_DIR / f"{emis_code}.png"
    if not img_path.exists():
        raise HTTPException(status_code=404, detail="No satellite image for this school")
    return FileResponse(img_path, media_type="image/png")


# =========================================================================
# Entrypoint
# =========================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
