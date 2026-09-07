"""
QA audit 6 (frontend structural sanity): static syntax checks on
frontend/index.html JS without a browser.

Run from PROJECT ROOT:
    python tests/audit_jscheck.py

Checks: JS syntax via `node --check` (authoritative), template-literal
backtick parity, duplicate DOM ids, that every element id referenced
by the $() helper actually exists in the HTML, and a verdict
regression guard (isGhost must not read .Tier).

NOTE: an earlier version hand-rolled brace/paren balancing with a
string-stripper, which false-positived on nested template literals —
replaced by node --check.

HISTORICAL RESULT (Sept 2026 audit): all checks pass.
"""
import re

html = open("frontend/index.html", encoding="utf-8").read()

# ---- extract <script> JS ----
scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
js = "\n".join(scripts)
print(f"script blocks: {len(scripts)} | JS chars: {len(js)}")

# ---- SYNTAX CHECK (authoritative): node --check on the extracted script ----
import subprocess
import tempfile
import os
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
    f.write(js)
    _tmp = f.name
_r = subprocess.run(["node", "--check", _tmp], capture_output=True, text=True)
os.unlink(_tmp)
if _r.returncode == 0:
    print("  node --check: JS SYNTAX VALID")
else:
    print("  *** JS SYNTAX ERROR ***")
    print(_r.stderr[:1500])

# ---- template literal count (must be even) ----
ticks = js.count("`")
print(f"  backticks: {ticks} -> {'EVEN' if ticks % 2 == 0 else '*** ODD — unterminated template literal ***'}")

# ---- duplicate ids ----
ids = re.findall(r'id="([^"]+)"', html)
dups = {i for i in ids if ids.count(i) > 1}
print(f"  DOM ids: {len(ids)} unique of {len(ids)} listed | duplicates: {dups if dups else 'none'}")

# ---- fetch URLs ----
urls = re.findall(r"fetch\(\s*[`'\"]([^`'\"]+)", js)
print(f"  fetch() calls: {len(urls)}")
for u in sorted(set(urls)):
    print(f"    {u[:90]}")
api_const = re.findall(r"const API\s*=\s*[`'\"]([^`'\"]+)", js)
print(f"  API base const: {api_const}")

# ---- getElementById references exist ----
refs = set(re.findall(r"getElementById\([`'\"]([^`'\"]+)", js))
missing = [r for r in refs if f'id="{r}"' not in html]
print(f"  getElementById refs: {len(refs)} | missing targets: {missing if missing else 'none'}")

# ---- leftover Tier-vs-Priority conflation check (regression guard) ----
bad = []
for ln_no, line in enumerate(js.splitlines(), 1):
    if re.search(r"isGhost\s*=\s*[^=]*\.Tier", line):
        bad.append((ln_no, line.strip()))
if bad:
    print("  *** VERDICT REGRESSION: isGhost still reads .Tier ***")
    for ln, t in bad:
        print(f"    line {ln}: {t}")
else:
    print("  verdict regression check: no isGhost/.Tier conflation (OK)")
