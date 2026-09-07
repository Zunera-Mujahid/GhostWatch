"""
test_config_guard.py
====================
Value guard for ghostwatch_config.py: freezes every scoring / classification
constant at its reviewed value so an accidental edit cannot silently change
school risk scores or Priority classifications.

If this test FAILS you either:
  * edited ghostwatch_config.py on purpose  -> follow the CHANGE PROTOCOL in
    its docstring (re-run the pipeline chain + audits), then update the
    frozen snapshot below to record the deliberate change;
  * did not edit anything                   -> someone/something else changed
    the config; inspect it before proceeding.

Run from anywhere:
    python tests/test_config_guard.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ghostwatch_config as cfg  # noqa: E402

# Frozen snapshot of the reviewed values (2026-09-06 config extraction).
FROZEN = {
    "PRIORITY_HIGH_MIN": 50,
    "PRIORITY_MEDIUM_MIN": 20,
    "SCORE_CAP": 100,
    "TIER1_WEIGHTS": {"attendance": 30, "illegal": 15, "toilet": 15,
                      "boundary": 10, "water": 10, "recency": 10,
                      "electricity": 5, "fill": 5},
    "TIER2_WEIGHTS": {"illegal": 35, "toilet": 25, "boundary": 15,
                      "water": 15, "electricity": 10},
    "RECENCY_CAP_DAYS": 365,
    "STATUS_OVERRIDE_STATES": ("Non-Functional", "Closed"),
    "STATUS_OVERRIDE_BONUS": 30,
    "FRAUD_SCORE_BOOST": 10,
    "FRAUD_PRIORITY_ESCALATION": {"Low": "Medium", "Medium": "High", "High": "High"},
    "ILLEGAL_PRIORITY_ESCALATION": {"Low": "Medium", "Medium": "High", "High": "High"},
    "SAT_MOD_SCHOOL": {"Low_Density": 15, "Mixed_Density": 7,
                       "High_Density": 0, "NoData": 0},
    "SAT_MOD_VILLAGE": {"Low_Density": 5, "Mixed_Density": 2,
                        "High_Density": 0, "NoData": 0},
    "SAT_MOD_TEHSIL": {"Low_Density": 0, "Mixed_Density": 0,
                       "High_Density": 0, "NoData": 0},
    "FRAUD_PRIORITY_ESCALATION_MERGE": {"Low": "Low", "Medium": "High", "High": "High"},
    "SAT_LOC_FLOOR_PRIORITY": {"Low": "Medium", "Medium": "Medium", "High": "High"},
    "SATELLITE_EPOCH_YEARS": (2017, 2023),
}


def main():
    failures = []

    # 1. Every frozen value must still exist and match exactly
    for name, expected in FROZEN.items():
        if not hasattr(cfg, name):
            failures.append(f"MISSING: ghostwatch_config.{name} was removed")
            continue
        actual = getattr(cfg, name)
        if actual != expected:
            failures.append(f"CHANGED: ghostwatch_config.{name}\n"
                            f"    expected: {expected!r}\n"
                            f"    actual:   {actual!r}")

    # 2. No unexpected NEW constants either (surprise additions get reviewed)
    known = set(FROZEN)
    extra = {n for n in dir(cfg)
             if not n.startswith("_") and n.upper() == n and n not in known}
    for n in sorted(extra):
        failures.append(f"UNREVIEWED: new constant ghostwatch_config.{n} = "
                        f"{getattr(cfg, n)!r} is not in the frozen snapshot")

    # 3. Structural invariants the pipeline depends on
    if sum(cfg.TIER1_WEIGHTS.values()) != 100:
        failures.append("TIER1_WEIGHTS must sum to 100 "
                        f"(got {sum(cfg.TIER1_WEIGHTS.values())})")
    if sum(cfg.TIER2_WEIGHTS.values()) != 100:
        failures.append("TIER2_WEIGHTS must sum to 100 "
                        f"(got {sum(cfg.TIER2_WEIGHTS.values())})")
    if cfg.PRIORITY_MEDIUM_MIN > cfg.PRIORITY_HIGH_MIN:
        failures.append("PRIORITY_MEDIUM_MIN must be <= PRIORITY_HIGH_MIN")
    for mod_table in (cfg.SAT_MOD_SCHOOL, cfg.SAT_MOD_VILLAGE, cfg.SAT_MOD_TEHSIL):
        if set(mod_table) != {"Low_Density", "Mixed_Density", "High_Density", "NoData"}:
            failures.append(f"modifier table {mod_table} must cover all four flags")
    if any(cfg.SAT_MOD_TEHSIL.values()):
        failures.append("Tehsil-Level modifiers must all be 0 "
                        "(tehsil centroids never move a score)")
    if cfg.SATELLITE_EPOCH_YEARS[0] >= cfg.SATELLITE_EPOCH_YEARS[1]:
        failures.append("SATELLITE_EPOCH_YEARS must be (earliest, recent) in order")

    if failures:
        print("=" * 70)
        print("CONFIG GUARD: FAIL")
        print("=" * 70)
        for f in failures:
            print(f"  - {f}")
        print("\nIf these changes are deliberate, follow the CHANGE PROTOCOL in")
        print("ghostwatch_config.py, then update the FROZEN snapshot in this test.")
        sys.exit(1)

    print(f"CONFIG GUARD: PASS  ({len(FROZEN)} constants frozen, "
          f"weights sum to 100, tehsil modifiers all zero)")


if __name__ == "__main__":
    main()
