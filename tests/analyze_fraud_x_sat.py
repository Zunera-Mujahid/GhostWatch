"""Follow-up: correct fraud-flag dtype handling, HD image availability for the
3 School-Level high-risk schools, density thresholds, and Tier coverage.

Read-only. Run from PROJECT ROOT."""
import os
import pandas as pd

df = pd.read_csv("ghostwatch_final_merged_v2.csv", dtype={"EMIS_Code": str})
df["_fraud"] = df["Qwen_Fraud_Flag"].astype(str).str.strip().str.lower() == "true"

print("=" * 88)
print("A. FRAUD-FLAGGED SCHOOLS (correct bool handling) — their satellite risk?")
print("=" * 88)
ff = df[df["_fraud"]]
print(f"  flagged schools: {len(ff)}")
for _, r in ff.iterrows():
    print(f"  {r['School_Name'][:40]:42s} conf={str(r['Satellite_Confidence']):13s} "
          f"density={str(r['Satellite_Density_Flag']):13s} Tier={r['Tier']} "
          f"score={r['Ghost_Risk_Score_Tier1']}")

print()
print("=" * 88)
print("B. TIER COVERAGE: do the 3 School-Level high-risk schools have inspector visits?")
print("=" * 88)
for emis in ["31230405", "37110078", "33250398"]:
    r = df[df["EMIS_Code"] == emis].iloc[0]
    print(f"  {r['School_Name'][:40]:42s} Tier={r['Tier']} | Notes={str(r['Notes'])[:30]!r} "
          f"| Monitoring_Date={r['Monitoring_Date']}")
t1 = df[df["Tier"] == "1"]
t2 = df[df["Tier"] == "2"]
print(f"\n  Tier 1 (has monitoring visits): {len(t1)} schools — flagged: {(t1['_fraud']).sum()}")
print(f"  Tier 2 (census only, NO visits): {len(t2)} schools — flagged: {(t2['_fraud']).sum()}")
print("  (notes-based fraud flag can only ever fire for Tier 1 schools)")

print()
print("=" * 88)
print("C. HD SATELLITE IMAGES for the 3 School-Level high-risk schools?")
print("=" * 88)
for emis in ["31230405", "37110078", "33250398"]:
    p = f"satellite_images/{emis}.png"
    r = df[df["EMIS_Code"] == emis].iloc[0]
    exists = os.path.exists(p)
    size = os.path.getsize(p) / 1024 if exists else 0
    print(f"  {emis} {r['School_Name'][:38]:40s} image={'YES ' + str(int(size)) + ' KB' if exists else 'NO'}")

print()
print("=" * 88)
print("D. DENSITY THRESHOLDS + the built-up gap")
print("=" * 88)
sampled = df[pd.to_numeric(df["Satellite_BuiltUp_Percent"], errors="coerce").notna()]
b = pd.to_numeric(sampled["Satellite_BuiltUp_Percent"], errors="coerce")
for flag in ["Low_Density", "Mixed_Density", "High_Density"]:
    sub = b[sampled["Satellite_Density_Flag"] == flag]
    if len(sub):
        print(f"  {flag:13s}: n={len(sub):3d}  built-up range {sub.min():.1f}% .. {sub.max():.1f}%")
print("  (gap between Low and High band shows where the thresholds sit)")

print()
print("=" * 88)
print("E. THE TWO 0%-BUILT-UP SCHOOL-LEVEL SCHOOLS: enrollment story")
print("=" * 88)
for emis in ["37110078", "33250398"]:
    r = df[df["EMIS_Code"] == emis].iloc[0]
    print(f"  {r['School_Name'][:40]} (EMIS {emis})")
    print(f"    claims {int(r['Total_Enrolled'])} enrolled, {int(r['Teachers_Filled'])} teachers, "
          f"built-up at exact location: {r['Satellite_BuiltUp_Percent']}%")
    print(f"    HD image: {'satellite_images/' + emis + '.png' if os.path.exists('satellite_images/' + emis + '.png') else 'missing'}")
