"""Analysis: School-Level (exact-coordinate) schools x satellite risk —
how strong is the 'high satellite risk at a known location' signal,
and how is it currently weighted?

Read-only investigation. Run from PROJECT ROOT:
    python tests/analyze_school_level_sat.py
"""
import pandas as pd

df = pd.read_csv("ghostwatch_final_merged_v2.csv", dtype={"EMIS_Code": str})
tt = pd.read_csv("ghostwatch_two_tier_scores.csv", dtype={"EMIS_Code": str})

print("=" * 88)
print("1. COORDINATE CONFIDENCE BREAKDOWN (all 300 schools)")
print("=" * 88)
print(df["Satellite_Confidence"].value_counts(dropna=False).to_string())

print()
print("=" * 88)
print("2. CROSS-TAB: confidence level x density flag (satellite RISK in brackets)")
print("=" * 88)
ct = pd.crosstab(df["Satellite_Confidence"].fillna("(none)"), df["Satellite_Density_Flag"].fillna("(none)"))
risk_note = {"High_Density": "Low risk", "Mixed_Density": "Medium risk",
             "Low_Density": "HIGH risk", "NoData": "no data", "(none)": "no data"}
ct.columns = [f"{c}\n[{risk_note.get(c, '')}]" for c in ct.columns]
print(ct.to_string())

print()
print("=" * 88)
print("3. THE 'HIGH SATELLITE RISK' (Low_Density) SCHOOLS — full detail")
print("=" * 88)
ld = df[df["Satellite_Density_Flag"] == "Low_Density"]
m = ld.merge(
    tt[["EMIS_Code", "Ghost_Risk_Score_Tier1", "Infrastructure_Screening_Score"]],
    on="EMIS_Code", suffixes=("", "_base"))
for _, r in m.sort_values("Infrastructure_Screening_Score", ascending=False).iterrows():
    final = r["Infrastructure_Screening_Score"]
    base = r["Infrastructure_Screening_Score_base"]
    sat_pts = final - base if pd.notna(final) and pd.notna(base) else None
    print(f"\n  {r['School_Name'][:44]} (EMIS {r['EMIS_Code']})")
    print(f"    District: {r['District']} | Tehsil: {r['Tehsil']}")
    print(f"    Confidence: {r['Satellite_Confidence']} | Tier: {r['Tier']} | Built-up: {r['Satellite_BuiltUp_Percent']}%")
    print(f"    Base score: {base} -> Final: {final} (satellite contributed +{sat_pts})")
    print(f"    Priority: {r['Priority']} | Fraud flag: {r['Qwen_Fraud_Flag']}")
    print(f"    Status: {r['School_Status']} | Enrolled: {r['Total_Enrolled']} | "
          f"Teachers filled: {r['Teachers_Filled']}/{r['Teachers_Sanctioned']}")
    print(f"    Sat reason: {str(r['Satellite_Risk_Reason'])[:100]}")

print()
print("=" * 88)
print("4. FRAUD-FLAGGED SCHOOLS: what satellite risk do THEY have?")
print("=" * 88)
ff = df[df["Qwen_Fraud_Flag"] == "True"]
for _, r in ff.iterrows():
    print(f"  {r['School_Name'][:40]:42s} conf={str(r['Satellite_Confidence']):13s} "
          f"flag={r['Satellite_Density_Flag']} risk_score={r['Ghost_Risk_Score_Tier1']}")

print()
print("=" * 88)
print("5. SCHOOL-LEVEL SCHOOLS: priority distribution by density flag")
print("=" * 88)
sl = df[df["Satellite_Confidence"] == "School-Level"]
print(pd.crosstab(sl["Satellite_Density_Flag"].fillna("(none)"), sl["Priority"]).to_string())

print()
print("=" * 88)
print("6. BUILT-UP % DISTRIBUTION for School-Level vs Village-Level (High_Density only)")
print("=" * 88)
for conf in ["School-Level", "Village-Level"]:
    sub = df[(df["Satellite_Confidence"] == conf) & (df["Satellite_Density_Flag"] == "High_Density")]
    b = pd.to_numeric(sub["Satellite_BuiltUp_Percent"], errors="coerce")
    print(f"  {conf}: n={len(sub)}, built-up min={b.min():.0f}% median={b.median():.0f}% max={b.max():.0f}%")

print()
print("=" * 88)
print("7. WHAT WOULD CHANGE IF School-Level + Low_Density ESCALATED to >= Medium?")
print("=" * 88)
cand = df[(df["Satellite_Confidence"] == "School-Level") & (df["Satellite_Density_Flag"] == "Low_Density")]
print(f"  affected schools: {len(cand)}")
for _, r in cand.iterrows():
    print(f"    {r['School_Name'][:44]:46s} score={r['Infrastructure_Screening_Score']:5} priority={r['Priority']}")
