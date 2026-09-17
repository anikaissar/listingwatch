"""One-off diagnostic: bucket visual_best_similarity across every platform's
diff CSV, to see the real distribution of scores instead of guessing at a
threshold from a single SKU. Run from the pipeline/ folder:
    python visual_similarity_histogram.py
"""
import csv
import os

FILES = [
    ("Flipkart", "qa_diff_flipkart_vs_d2c.csv"),
    ("Nykaa", "nykaa_diff_latest.csv"),
    ("Myntra", "myntra_diff_latest.csv"),
    ("Amazon", "amazon_diff_latest.csv"),
]

BUCKETS = [
    (0.0, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6),
    (0.6, 0.65), (0.65, 0.7), (0.7, 0.75), (0.75, 0.8),
    (0.8, 0.85), (0.85, 0.9), (0.9, 0.95), (0.95, 1.001),
]

for label, fname in FILES:
    if not os.path.exists(fname):
        print(f"{label}: {fname} not found, skipping")
        continue
    vals = []
    with open(fname, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            v = row.get("visual_best_similarity", "")
            if v not in (None, ""):
                try:
                    vals.append(float(v))
                except ValueError:
                    pass
    print(f"\n{label}: {len(vals)} rows with a visual_best_similarity value")
    if not vals:
        continue
    for lo, hi in BUCKETS:
        n = sum(1 for v in vals if lo <= v < hi)
        bar = "#" * n
        print(f"  {lo:.2f}-{hi:.2f}: {n:4d}  {bar}")
    print(f"  min={min(vals):.3f}  max={max(vals):.3f}  "
          f"mean={sum(vals)/len(vals):.3f}")
