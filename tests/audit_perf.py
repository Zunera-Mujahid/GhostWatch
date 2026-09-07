"""
QA audit 5 (performance): API timings for the live demo.

Run from PROJECT ROOT (requires backend running on :8000):
    python tests/audit_perf.py

HISTORICAL RESULT (Sept 2026 audit): cold load 28 ms, school click
incl. 1 MB HD image 77 ms, one page of 25 schools 15-43 ms — no
stage risk. Only warning: ghostwatch_map.html needs internet for
OpenStreetMap tiles; the dashboard itself is fully offline.
"""
import os
import statistics
import time
import urllib.request

BASE = "http://127.0.0.1:8000"

def timed(path, n=5):
    times = []
    size = 0
    for _ in range(n):
        t0 = time.perf_counter()
        r = urllib.request.urlopen(BASE + path)
        body = r.read()
        times.append(time.perf_counter() - t0)
        size = len(body)
    return min(times) * 1000, statistics.mean(times) * 1000, size

print("=" * 84)
print("API PERFORMANCE (n=5 each, ms)")
print("=" * 84)
tests = [
    ("/schools?limit=25 (one dashboard page)", "/schools?limit=25"),
    ("/schools?limit=25&sort=flagged_first", "/schools?limit=25&sort=flagged_first"),
    ("/schools?limit=100 (bulk)", "/schools?limit=100"),
    ("/schools/35220032 (detail)", "/schools/35220032"),
    ("/stats", "/stats"),
    ("/districts", "/districts"),
    ("/satellite-image/35220159 (1MB HD png)", "/satellite-image/35220159"),
]
for label, path in tests:
    best, avg, size = timed(path)
    print(f"  {label:42s} best={best:7.1f}ms  avg={avg:7.1f}ms  {size/1024:8.1f} KB")

print()
print("=" * 84)
print("STATIC ASSET WEIGHT")
print("=" * 84)
for f in ["frontend/index.html", "ghostwatch_map.html"]:
    kb = os.path.getsize(f) / 1024
    print(f"  {f:28s} {kb:8.1f} KB")
img_dir = "satellite_images"
sizes = [os.path.getsize(os.path.join(img_dir, f)) for f in os.listdir(img_dir)]
print(f"  satellite_images/            {len(sizes)} PNGs, avg {statistics.mean(sizes)/1024:.0f} KB, "
      f"max {max(sizes)/1024:.0f} KB")
total_kb = sum(sizes) / 1024
print(f"  (only 1 image loads per school click - lazy loading; total folder {total_kb/1024:.1f} MB)")

# Simulate dashboard cold load: stats + districts + first page
t0 = time.perf_counter()
for path in ["/stats", "/districts", "/schools?limit=25"]:
    urllib.request.urlopen(BASE + path).read()
cold = (time.perf_counter() - t0) * 1000
print(f"\n  Simulated dashboard cold load (stats+districts+first page): {cold:.0f} ms")

# Simulate clicking a school: detail + image
t0 = time.perf_counter()
urllib.request.urlopen(BASE + "/schools/35220032").read()
urllib.request.urlopen(BASE + "/satellite-image/35220032").read()
click = (time.perf_counter() - t0) * 1000
print(f"  Simulated school click (detail + HD image): {click:.0f} ms")
