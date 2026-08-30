"""
analyze_risk.py
Reads Punjab school data, ranks schools by Government_Monitoring_Risk_Score,
prints the top 10 most suspicious schools, and saves them to CSV.
"""

import pandas as pd

INPUT_FILE  = "ghost_school_detector_full_dataset.csv"
OUTPUT_FILE = "top_suspicious_schools.csv"

# ── 1. Load data ──────────────────────────────────────────────────────────────
df = pd.read_csv(INPUT_FILE)

# ── 2. Sort by risk score (highest risk first) ────────────────────────────────
df_sorted = df.sort_values(
    by="Government_Monitoring_Risk_Score",
    ascending=False
).reset_index(drop=True)

# ── 3. Select the columns we care about ───────────────────────────────────────
cols = ["School_Name", "District", "Government_Monitoring_Risk_Score", "Audit_Priority"]
top10 = df_sorted[cols].head(10).copy()
top10.index = range(1, 11)          # 1-based rank for display
top10.index.name = "Rank"

# ── 4. Print a readable table ─────────────────────────────────────────────────
print("\n" + "=" * 66)
print("   TOP 10 MOST SUSPICIOUS SCHOOLS  (by Risk Score, desc)")
print("=" * 66 + "\n")
print(
    top10.to_string(
        float_format=lambda x: f"{x:.2f}",
        col_space=22,
    )
)
print()

# ── 5. Save to CSV ────────────────────────────────────────────────────────────
top10.to_csv(OUTPUT_FILE)
print(f"[OK] Results saved to: {OUTPUT_FILE}")
