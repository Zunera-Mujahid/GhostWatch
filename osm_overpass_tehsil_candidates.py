"""
osm_overpass_tehsil_candidates.py
=================================
Candidate discovery for the 46 still-unmapped schools using the free,
public OpenStreetMap Overpass API (no signup, no API key, no card).

Method -- GEOGRAPHIC, not text search (unlike the earlier
Nominatim/Geoapify school-name geocoding):
  Phase 1  For each unmapped school's district, take the median coordinate
           of the district's ALREADY-geocoded sibling schools (from the
           247 mapped schools in ghostwatch_final_merged_v2.csv) and run
           one Overpass `is_in(point)` query to find the enclosing
           administrative-boundary relation by pure geometry (no name
           matching -- Pakistani boundary relations often carry only
           Urdu names, which defeats English text search) plus its
           bounding box.  The resolved name is validated against the
           dataset district; on mismatch (or for districts that span
           newer OSM splits, e.g. the pre-split 'D.G. KHAN' district vs
           OSM's separate Taunsa District), the enclosing admin_level=5
           DIVISION relation is used instead so the pool covers the
           whole dataset district.
  Phase 2  Query ALL amenity=school elements (nodes + ways) inside each
           district's bounding box -- an indexed, fast geographic query.
           (District scope is used instead of tehsil scope because OSM's
           tehsil polygons proved unreliable for this dataset: the
           Liaqatpur-tehsil schools' own coordinates fall inside OSM's
           'Khan Pur Tehsil' polygon.  District boundaries are stable,
           and the true match is always within the district.)
  Phase 3  Score every OSM school against the dataset school name
           (token overlap / containment / string similarity), add the
           distance from the school's tehsil sibling-median point as a
           proximity hint, and save ranked candidates for MANUAL review.
           Unnamed amenity=school POIs near the tehsil hint are also
           listed -- an unnamed school POI is still a reviewable lead.

IMPORTANT -- NOTHING IS AUTO-ACCEPTED:
  * This script writes NO coordinates into any pipeline file.
  * Output is a review sheet only: osm_overpass_tehsil_candidates.csv
  * Every match (fuzzy or unnamed) must be human-verified via the OSM_URL
    before its coordinates enter the pipeline.

RUN: python osm_overpass_tehsil_candidates.py
"""

import json
import math
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd
import requests

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
READY_CSV = ROOT / "schools_needing_manual_geocoding_READY (1).csv"
FINAL_CSV = ROOT / "ghostwatch_final_merged_v2.csv"
OUT_CSV = ROOT / "osm_overpass_tehsil_candidates.csv"
CACHE_JSON = ROOT / "_overpass_cache.json"

HEADERS = {"User-Agent": "GhostWatch/1.0 (school geocoding research)"}

# Main endpoint proved the most reliable for light/medium queries in tests;
# mirrors rotate in as fallbacks.
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# Districts whose DATASET boundary spans multiple current OSM districts
# because of recent administrative splits: the sibling-median is_in then
# resolves to only ONE of the split pieces (e.g. the pre-split 'D.G. KHAN'
# district -> OSM 'Taunsa District'), missing the rest.  For these, the
# enclosing admin_level=5 DIVISION relation (a superset) is used instead.
FORCE_DIVISION_DISTRICTS = {"D.G. KHAN"}

# Squashed-name aliases used to VALIDATE that the relation resolved by
# is_in really is the dataset's district (squashing = lowercase
# alphanumerics only, with district/division/tehsil words removed).
DISTRICT_NAME_ALIASES = {
    "D.G. KHAN": {"dgkhan", "deraghazikhan"},
    "T.T.SINGH": {"ttsingh", "tobateksingh"},
}

# Words dropped before name comparison (government school-type words)
STOPWORDS = {
    "govt", "government", "gps", "ggps", "gghs", "ghss", "ghs", "gms",
    "ggms", "ges", "gces", "gpes", "gss", "gcs", "mc", "municipal",
    "committee", "boys", "girls", "boy", "girl", "primary", "elementary",
    "middle", "high", "higher", "secondary", "school", "schools",
    "public", "the", "and", "of", "no", "number", "model",
}

NAMED_PER_SCHOOL = 6     # top-N named candidates saved per school
UNNAMED_PER_SCHOOL = 3   # nearest unnamed POIs saved per school

_CONSEC_FAILURES = 0


# ---------------------------------------------------------------------------
# Overpass helpers
# ---------------------------------------------------------------------------
def overpass(query, tag=""):
    """POST one Overpass QL query, rotating endpoints on failure.

    A 200 response carrying an error/timeout 'remark' is treated as a soft
    failure and retried.  Returns parsed JSON or None on total failure.
    """
    global _CONSEC_FAILURES
    for attempt in range(1, 5):
        ep = OVERPASS_ENDPOINTS[(attempt - 1) % len(OVERPASS_ENDPOINTS)]
        host = ep.split("/")[2]
        try:
            resp = requests.post(ep, data={"data": query}, headers=HEADERS,
                                 timeout=240)
            if resp.status_code == 200:
                data = resp.json()
                remark = str(data.get("remark", ""))
                if "error" in remark.lower() or "timed out" in remark.lower():
                    print(f"    [{tag}] soft failure ({remark[:60]}); "
                          f"retrying in {8 * attempt}s")
                else:
                    _CONSEC_FAILURES = 0
                    return data
            elif resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 0)) or 30 * attempt
                print(f"    [{tag}] HTTP 429 from {host}; waiting {wait}s")
                time.sleep(wait)
                continue
            else:
                print(f"    [{tag}] HTTP {resp.status_code} from {host}; "
                      f"retrying in {8 * attempt}s")
        except Exception as exc:
            print(f"    [{tag}] {host} error: {type(exc).__name__}: {exc}; "
                  f"retrying in {8 * attempt}s")
        time.sleep(8 * attempt)
    print(f"    [{tag}] all endpoints failed")
    _CONSEC_FAILURES += 1
    if _CONSEC_FAILURES >= 4:
        raise RuntimeError("Overpass API unreachable after 4 consecutive "
                           "total failures - aborting")
    return None


def fetch_admin_at_point(lat, lon):
    """
    One light Overpass query: every administrative-boundary relation that
    encloses (lat, lon), with tags and bounding box.

    Returns list of dicts {id, name, name_en, admin_level, bounds} or None.
    """
    query = (f"[out:json][timeout:60];\n"
             f"is_in({lat},{lon}) -> .a;\n"
             f'rel(pivot.a)["boundary"="administrative"];\n'
             f"out bb;")
    data = overpass(query, tag=f"is_in {lat:.4f},{lon:.4f}")
    if data is None:
        return None
    out = []
    for e in data.get("elements", []):
        if e.get("type") != "relation":
            continue
        tags = e.get("tags") or {}
        try:
            lvl = int(str(tags.get("admin_level", "-1")))
        except ValueError:
            lvl = -1
        out.append({
            "id": e["id"],
            "name": str(tags.get("name", "")).strip(),
            "name_en": str(tags.get("name:en", "")).strip(),
            "admin_level": lvl,
            "bounds": e.get("bounds"),
        })
    return out


def fetch_schools_in_bbox(bounds):
    """All amenity=school nodes and ways inside a bounding box (fast path)."""
    bb = (f"{bounds['minlat']},{bounds['minlon']},"
          f"{bounds['maxlat']},{bounds['maxlon']}")
    query = (f"[out:json][timeout:120];\n"
             f"(\n"
             f'  node["amenity"="school"]({bb});\n'
             f'  way["amenity"="school"]({bb});\n'
             f");\n"
             f"out center;")
    data = overpass(query, tag=f"bbox schools {bb}")
    if data is None:
        return None
    schools = []
    for e in data.get("elements", []):
        if e.get("type") == "relation":
            continue                      # keep nodes + ways only
        tags = e.get("tags") or {}
        lat = e.get("lat", (e.get("center") or {}).get("lat"))
        lon = e.get("lon", (e.get("center") or {}).get("lon"))
        if lat is None or lon is None:
            continue
        schools.append({
            "osm_type": e.get("type"),
            "osm_id": e.get("id"),
            "name": str(tags.get("name") or tags.get("name:en") or "").strip(),
            "lat": float(lat),
            "lon": float(lon),
        })
    return schools


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------
def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def norm_tokens(name):
    s = str(name).lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return [t for t in s.split() if t and t not in STOPWORDS]


def similarity(ta, tb):
    """Return (jaccard, containment, string_ratio, combined_score)."""
    sa, sb = set(ta), set(tb)
    if not sa or not sb:
        return 0.0, 0.0, 0.0, 0.0
    inter = sa & sb
    jac = len(inter) / len(sa | sb)
    cont = len(inter) / min(len(sa), len(sb))
    seq = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    return jac, cont, seq, max(jac, cont, seq)


def tier(score):
    if score >= 0.70:
        return "Strong"
    if score >= 0.40:
        return "Possible"
    return "Weak"


def squash_name(name):
    """Lowercase alphanumerics only, admin-suffix words removed."""
    s = str(name).lower()
    for w in ("district", "division", "tehsil"):
        s = s.replace(w, "")
    return re.sub(r"[^a-z0-9]", "", s)


def district_name_ok(expected, relation):
    """True if the resolved relation plausibly IS the expected district."""
    ok = {squash_name(expected)} | DISTRICT_NAME_ALIASES.get(expected, set())
    names = [n for n in (relation.get("name_en"), relation.get("name")) if n]
    return any(squash_name(n) in ok for n in names)


def pick_level(adms, level):
    """Administrative relations at the given level that carry a bbox."""
    return [a for a in adms if a["admin_level"] == level and a["bounds"]]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 80)
    print("GhostWatch - OSM Overpass geographic school-candidate discovery")
    print("(free public Overpass API; geographic query, NOT text search)")
    print("=" * 80)

    # ---- Load the 46 unmapped schools + geocoded sibling pool -------------
    ready = pd.read_csv(READY_CSV, dtype={"EMIS_Code": str})
    schools = ready[ready["Latitude"].isna() | ready["Longitude"].isna()].copy()
    print(f"\nUnmapped schools in READY file: {len(schools)}")

    final = pd.read_csv(FINAL_CSV, dtype={"EMIS_Code": str})
    geo = final[(final["Data_Type"].astype(str).str.strip() != "Synthetic")
                & final["Latitude"].notna()].copy()
    real_unmapped = final[(final["Data_Type"].astype(str).str.strip() != "Synthetic")
                          & final["Latitude"].isna()]
    if set(real_unmapped["EMIS_Code"]) == set(schools["EMIS_Code"]):
        print("Cross-check OK: same schools as the real no-coordinate rows "
              "in ghostwatch_final_merged_v2.csv")
    else:
        print(f"WARNING: READY unmapped set ({len(schools)}) differs from final "
              f"CSV real unmapped rows ({len(real_unmapped)})")
    print(f"Geocoded sibling pool: {len(geo)} schools")

    cache = {}
    if CACHE_JSON.exists():
        try:
            cache = json.loads(CACHE_JSON.read_text(encoding="utf-8"))
            print(f"Cache loaded: {len(cache)} entries")
        except Exception:
            cache = {}

    # ---- Phase 1: district boundary relations via is_in --------------------
    districts = sorted(schools["District"].astype(str).str.strip().unique())
    print(f"\nPHASE 1: resolving {len(districts)} districts to OSM boundary "
          f"relations via is_in (geographic, no name matching)")
    district_area = {}
    for d in districts:
        cache_key = f"district::{d}"
        if cache_key in cache:
            district_area[d] = cache[cache_key]
            a = district_area[d]
            print(f"  {d:20s} (cached) -> rel {a['rel_id']} "
                  f"{a['name_en'] or a['name']} (admin_level={a['admin_level']})")
            continue
        sibs = geo[geo["District"] == d]
        if sibs.empty:
            print(f"  {d:20s} -> NO geocoded siblings, skipped")
            continue
        lat = float(sibs["Latitude"].median())
        lon = float(sibs["Longitude"].median())
        admins = fetch_admin_at_point(lat, lon)
        if admins is None:
            print(f"  {d:20s} -> is_in FAILED (will not be retried here)")
            continue
        # Districts spanning newer OSM splits resolve to only part of the
        # dataset district -> use the enclosing division (level 5) instead.
        want_level = 5 if d in FORCE_DIVISION_DISTRICTS else 6
        picked = pick_level(admins, want_level)
        if not picked:
            # retry once with the mean point
            lat2 = float(sibs["Latitude"].mean())
            lon2 = float(sibs["Longitude"].mean())
            admins = fetch_admin_at_point(lat2, lon2)
            if admins is not None:
                picked = pick_level(admins, want_level)
        if not picked:
            print(f"  {d:20s} -> no admin_level={want_level} boundary found "
                  f"at sibling median ({lat:.4f},{lon:.4f})")
            continue
        a = picked[0]
        note = ""
        if d in FORCE_DIVISION_DISTRICTS:
            note = " [division: dataset district spans newer OSM districts]"
        elif not district_name_ok(d, a):
            # unexpected name mismatch -> widen to the division (superset)
            div = pick_level(admins, 5)
            if div:
                a = div[0]
                note = (f" [name mismatch -> widened to division "
                        f"'{a['name_en'] or a['name']}']")
            else:
                note = " [WARNING: resolved name mismatch, kept level-6 area]"
        district_area[d] = {"rel_id": a["id"], "name": a["name"],
                            "name_en": a["name_en"],
                            "admin_level": a["admin_level"],
                            "bounds": a["bounds"]}
        cache[cache_key] = district_area[d]
        CACHE_JSON.write_text(json.dumps(cache), encoding="utf-8")
        print(f"  {d:20s} -> rel {a['id']} {a['name_en'] or a['name']} "
              f"(admin_level={a['admin_level']}, bbox ok){note}")
        time.sleep(3)

    # ---- Phase 2: amenity=school pools per district bbox -------------------
    print(f"\nPHASE 2: Overpass geographic query - amenity=school inside each "
          f"district bbox")
    pools = {}
    fetch_failed = False
    for d, a in sorted(district_area.items()):
        cache_key = f"pool::{a['rel_id']}"
        if cache_key in cache:
            pools[a["rel_id"]] = cache[cache_key]
            print(f"  {d:20s} (cached) -> {len(pools[a['rel_id']])} school POIs")
            continue
        pool = fetch_schools_in_bbox(a["bounds"])
        if pool is None:
            # do NOT cache failures -- a re-run should retry them
            print(f"  {d:20s} -> FETCH FAILED (reported as no candidates; "
                  f"not cached, re-run retries)")
            pools[a["rel_id"]] = []
            fetch_failed = True
            time.sleep(3)
            continue
        if not pool:
            print(f"  {d:20s} -> empty pool, retrying once...")
            time.sleep(10)
            pool2 = fetch_schools_in_bbox(a["bounds"])
            if pool2:
                pool = pool2
        named = sum(1 for s in pool if s["name"])
        print(f"  {d:20s} -> {len(pool)} school POIs ({named} named)")
        pools[a["rel_id"]] = pool
        cache[cache_key] = pool
        CACHE_JSON.write_text(json.dumps(cache), encoding="utf-8")
        time.sleep(3)

    # ---- Phase 3: score candidates per school ------------------------------
    print(f"\nPHASE 3: scoring candidates (top {NAMED_PER_SCHOOL} named + "
          f"nearest {UNNAMED_PER_SCHOOL} unnamed per school)")
    cand_rows = []
    school_stats = []
    for _, s in schools.iterrows():
        emis = s["EMIS_Code"]
        dist = str(s["District"]).strip()
        tehs = str(s["Tehsil"]).strip()
        area = district_area.get(dist)
        stat = {"emis": emis, "name": s["School_Name"], "district": dist,
                "tehsil": tehs, "area": "-", "pool": 0,
                "n_cand": 0, "best": 0.0, "best_name": ""}

        # proximity reference: tehsil sibling median, else district median
        ref_lat = ref_lon = None
        ref_kind = ""
        t_sibs = geo[(geo["District"] == dist) & (geo["Tehsil"] == tehs)]
        if not t_sibs.empty:
            ref_lat = float(t_sibs["Latitude"].median())
            ref_lon = float(t_sibs["Longitude"].median())
            ref_kind = f"tehsil sibling median ({len(t_sibs)} schools)"
        elif area:
            d_sibs = geo[geo["District"] == dist]
            ref_lat = float(d_sibs["Latitude"].median())
            ref_lon = float(d_sibs["Longitude"].median())
            ref_kind = f"district sibling median ({len(d_sibs)} schools)"

        if area:
            pool = pools.get(area["rel_id"], [])
            kind = "division" if area["admin_level"] == 5 else "district"
            stat["area"] = (f"{kind} bbox ({area['name_en'] or area['name']}, "
                            f"rel {area['rel_id']})")
            stat["pool"] = len(pool)
            ta = norm_tokens(s["School_Name"])

            scored, unnamed = [], []
            for p in pool:
                dist_km = (haversine_km(ref_lat, ref_lon, p["lat"], p["lon"])
                           if ref_lat is not None else None)
                if p["name"]:
                    jac, cont, seq, sc = similarity(ta, norm_tokens(p["name"]))
                    if sc > 0.05:
                        scored.append((sc, jac, cont, seq, dist_km, p))
                else:
                    unnamed.append((dist_km, p))

            scored.sort(key=lambda x: (-x[0], x[5]["osm_id"] or 0))
            unnamed.sort(key=lambda x: (x[0] if x[0] is not None else 1e9,
                                        x[1]["osm_id"] or 0))
            stat["n_cand"] = len(scored)

            def _row(sc, jac, cont, seq, dist_km, p, tier_label):
                return {
                    "EMIS_Code": emis,
                    "School_Name": s["School_Name"],
                    "District": s["District"],
                    "Tehsil": s["Tehsil"],
                    "Area_Used": stat["area"],
                    "OSM_Type": p["osm_type"],
                    "OSM_ID": p["osm_id"],
                    "OSM_Name": p["name"] or "(unnamed amenity=school POI)",
                    "OSM_Latitude": round(p["lat"], 6),
                    "OSM_Longitude": round(p["lon"], 6),
                    "OSM_URL": f"https://www.openstreetmap.org/"
                               f"{p['osm_type']}/{p['osm_id']}",
                    "Distance_km": (round(dist_km, 1)
                                    if dist_km is not None else ""),
                    "Distance_Reference": ref_kind,
                    "Similarity_Score": round(sc, 3),
                    "Token_Jaccard": round(jac, 3),
                    "Token_Containment": round(cont, 3),
                    "String_Ratio": round(seq, 3),
                    "Confidence_Tier": tier_label,
                    "Reviewer_Decision": "",
                    "Status": "NEEDS MANUAL VERIFICATION - not accepted",
                }

            for sc, jac, cont, seq, dist_km, p in scored[:NAMED_PER_SCHOOL]:
                cand_rows.append(_row(sc, jac, cont, seq, dist_km, p, tier(sc)))
            if ref_lat is not None:      # unnamed POIs need a distance context
                for dist_km, p in unnamed[:UNNAMED_PER_SCHOOL]:
                    cand_rows.append(_row(0.0, 0.0, 0.0, 0.0, dist_km, p,
                                          "Unnamed_POI"))
            if scored:
                stat["best"] = round(scored[0][0], 3)
                stat["best_name"] = scored[0][5]["name"]
        school_stats.append(stat)

    # ---- Save candidate sheet ----------------------------------------------
    cand = pd.DataFrame(cand_rows)
    if not cand.empty:
        cand = cand.sort_values(
            ["EMIS_Code", "Similarity_Score", "Distance_km"],
            ascending=[True, False, True]).reset_index(drop=True)
    cand.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"\nSaved candidate sheet: {OUT_CSV.name} ({len(cand)} rows)")

    # ---- Report -------------------------------------------------------------
    print("\n" + "=" * 80)
    print("REPORT (candidates only -- NOTHING auto-accepted)")
    print("=" * 80)

    n_res = len(district_area)
    print(f"\nDistrict boundary resolution: {n_res}/{len(districts)} districts "
          f"resolved via geographic is_in")

    n_any = sum(1 for st in school_stats if st["n_cand"] > 0)
    if not cand.empty:
        emis_strong = set(cand.loc[cand["Confidence_Tier"] == "Strong",
                                   "EMIS_Code"])
        emis_poss = set(cand.loc[cand["Confidence_Tier"].isin(
            ["Strong", "Possible"]), "EMIS_Code"])
        emis_unnamed = set(cand.loc[cand["Confidence_Tier"] == "Unnamed_POI",
                                    "EMIS_Code"])
    else:
        emis_strong = emis_poss = emis_unnamed = set()
    total = len(schools)
    print(f"\nCandidate matches for the {total} unmapped schools:")
    print(f"  schools with >=1 named candidate (score>0.05)     : {n_any}/{total}")
    print(f"  schools with >=1 STRONG named candidate (>=0.70)  : {len(emis_strong)}/{total}")
    print(f"  schools with >=1 POSSIBLE+ named candidate (>=0.40): {len(emis_poss)}/{total}")
    print(f"  schools with >=1 unnamed nearby POI listed         : {len(emis_unnamed)}/{total}")
    print(f"  schools with NO candidates at all                 : "
          f"{total - max(n_any, len(emis_unnamed))}/{total}")

    print(f"\nPer-school detail (best named candidate; full list in CSV):")
    for st in school_stats:
        print(f"  {st['emis']} {str(st['name'])[:36]:36s} "
              f"[{str(st['district'])[:14]:14s}/{str(st['tehsil'])[:14]:14s}] "
              f"pool={st['pool']:>3} named_cands={st['n_cand']:>2} "
              f"best={st['best']:.2f} {str(st['best_name'])[:34]}")

    if fetch_failed or len(district_area) < len(districts):
        print("\nCache kept (_overpass_cache.json): re-run the script to "
              "retry failed fetches -- cached results are reused.")
    else:
        try:
            CACHE_JSON.unlink()
            print("\nTemp cache removed.")
        except OSError:
            print("\nTemp cache could not be removed (ignored).")

    print("\nIMPORTANT: all candidates are UNVERIFIED leads for manual review.")
    print("Open each OSM_URL, confirm the school identity, and only then add")
    print("coordinates to the READY file (same process as the 11-school batch).")
    print("\nDONE.")


if __name__ == "__main__":
    main()
