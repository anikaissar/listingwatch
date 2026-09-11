#!/usr/bin/env python3
"""
Nykaa-vs-D2C diff engine -- Nykaa's counterpart to qa_diff.py (which does the
same job for Flipkart). Deliberately imports the shared pure comparison
functions from qa_diff.py (description_similarity, title_sanity,
ingredients_coverage, image_count_flag, visual_similarity, normalize_text)
rather than re-implementing them, so a fix made there (e.g. a stopword
tweak, a boilerplate-stripping fix) automatically applies to both platforms
and the two never drift apart on shared logic. Only what's genuinely
different about Nykaa lives in this file.

Inputs:
  observed_nykaa_latest.csv  -- from nykaa_pdp_scraper.py
  nathabit_reference_latest.csv -- from nathabit_pdp_scraper.py (the SAME
                                    D2C reference file the Flipkart diff
                                    uses -- it's keyed by nh_sku, which is
                                    shared across every platform, so there's
                                    no separate D2C scrape needed per
                                    platform)

Join key: nh_sku.

What's DIFFERENT from Flipkart's diff, and why:
  - No shelf-life comparison at all. Confirmed (2026-09-11, 45/45 real
    Nykaa products sampled) that Nykaa's own structured expiry field is
    null on every product checked -- there's nothing on Nykaa's side to
    diff against the D2C shelf life, so this column doesn't exist here
    (unlike Flipkart's shelf_life_status, which compares against a real
    Flipkart "Maximum Shelf Life" spec field).
  - "nykaa_page_broken" covers TWO real, confirmed cases instead of
    Flipkart's one: pdp_availability == "page_not_found" (Nykaa's own
    404/isNotFound signal -- see nykaa_pdp_scraper.py's finding on product
    10346740) OR pdp_availability == "no_content" (extraction found no
    title/JSON-LD/meta at all -- nothing usable to compare, same practical
    effect as Flipkart's generic placeholder page). Both leave every
    title/description/image comparison column blank, same as Flipkart's
    handling of flipkart_page_broken.
  - "unavailable" (real page, genuinely out of stock) is NOT treated as
    broken -- same as Flipkart's behavior -- because the page still has
    real content to compare (title, images, description), it's just out
    of stock right now.

What each output column means: see qa_diff.py's module docstring for the
shared columns (title_*, desc_*, ingredients_coverage, image_count_flag,
visual_*) -- the logic is identical, just applied to nykaa_title/
nykaa_image_count/etc instead of flipkart_title/flipkart_image_count.

Usage:
    python nykaa_qa_diff.py --selftest
    python nykaa_qa_diff.py --nykaa observed_nykaa_latest.csv --d2c nathabit_reference_latest.csv --out nykaa_diff_latest.csv
    python nykaa_qa_diff.py --visual   # also downloads + compares images, needs network + pillow
"""

import argparse
import csv
import os
import sys
import time

import qa_diff  # shared pure comparison functions live here

NYKAA_DEFAULT = "observed_nykaa_latest.csv"
D2C_DEFAULT = "nathabit_reference_latest.csv"
OUT_DEFAULT = "nykaa_diff_latest.csv"
VISUAL_CACHE_DIR = "qa_diff_image_cache"  # shared cache dir with qa_diff.py -- same D2C images get reused, no re-download

OUT_COLS = [
    "nh_sku", "nykaa_product_id", "pdp_availability", "nykaa_page_broken",
    "nykaa_title", "d2c_title",
    "title_seq_ratio", "title_d2c_term_coverage", "title_plausible",
    "desc_seq_ratio", "desc_token_jaccard",
    "ingredients_coverage",
    "nykaa_extraction_source",
    "nykaa_image_count", "d2c_image_count", "image_count_flag",
    "visual_best_similarity", "visual_avg_similarity",
]


def is_nykaa_page_broken(pdp_availability):
    """True when there's nothing real to compare against the D2C
    reference: either Nykaa's own 404/isNotFound signal fired
    (pdp_availability == "page_not_found"), or extraction found no title/
    JSON-LD/meta at all (pdp_availability == "no_content"). Genuinely
    out-of-stock-but-real ("unavailable") is NOT broken -- that page still
    has real content worth comparing."""
    return pdp_availability in ("page_not_found", "no_content")


# ===========================================================================
# Orchestration
# ===========================================================================
def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run(nykaa_path, d2c_path, out_path, do_visual=False):
    if not os.path.exists(nykaa_path):
        sys.exit(f"{nykaa_path} not found -- run nykaa_pdp_scraper.py first.")
    if not os.path.exists(d2c_path):
        sys.exit(f"{d2c_path} not found -- run nathabit_pdp_scraper.py first "
                  f"(the same D2C reference file the Flipkart diff uses).")

    nykaa_rows = load_csv(nykaa_path)
    d2c_rows = load_csv(d2c_path)
    d2c_by_sku = {r["nh_sku"]: r for r in d2c_rows if r.get("nh_sku")}
    print(f"Loaded {len(nykaa_rows)} Nykaa rows and {len(d2c_rows)} D2C reference rows.", flush=True)

    client = None
    if do_visual:
        import httpx
        client = httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)

        all_urls = []
        for nk in nykaa_rows:
            if is_nykaa_page_broken(nk.get("pdp_availability", "")):
                continue
            all_urls.extend(u for u in (nk.get("obs_image_urls") or "").split("|") if u)
            d2c = d2c_by_sku.get(nk.get("nh_sku", ""))
            if d2c:
                all_urls.extend(u for u in (d2c.get("image_urls") or "").split("|") if u)
        qa_diff._prefetch_images(all_urls, VISUAL_CACHE_DIR, client)

    unmatched = []
    out_rows = []
    t_start = time.time()
    n_total = len(nykaa_rows)
    for i, nk in enumerate(nykaa_rows, 1):
        if i % 50 == 0 or i == n_total:
            print(f"  diffing row {i}/{n_total} -- {time.time() - t_start:.0f}s elapsed", flush=True)
        nh_sku = nk.get("nh_sku", "")
        d2c = d2c_by_sku.get(nh_sku)
        if not d2c:
            unmatched.append(nh_sku)
            continue

        broken = is_nykaa_page_broken(nk.get("pdp_availability", ""))
        if broken:
            desc_sim = {"seq_ratio": "", "token_jaccard": ""}
            title_sim = {"seq_ratio": "", "d2c_term_coverage": "", "plausible": ""}
            ing_cov = ""
        else:
            desc_sim = qa_diff.description_similarity(nk.get("obs_description_text", ""), d2c.get("description", ""))
            title_sim = qa_diff.title_sanity(nk.get("obs_title", ""), d2c.get("title", ""))
            ing_cov = qa_diff.ingredients_coverage(nk.get("obs_description_text", ""), d2c.get("ingredients", ""))
            ing_cov = ing_cov if ing_cov is not None else ""
        img_flag = ("nykaa_page_broken" if broken
                    else qa_diff.image_count_flag(nk.get("obs_image_count"), d2c.get("image_count")))

        visual_best = visual_avg = ""
        if do_visual and not broken:
            nk_urls = [u for u in (nk.get("obs_image_urls") or "").split("|") if u]
            d2c_urls = [u for u in (d2c.get("image_urls") or "").split("|") if u]
            best, avg = qa_diff.visual_similarity(nk_urls, d2c_urls, VISUAL_CACHE_DIR, client)
            visual_best = best if best is not None else ""
            visual_avg = avg if avg is not None else ""

        out_rows.append({
            "nh_sku": nh_sku, "nykaa_product_id": nk.get("nykaa_product_id", ""),
            "pdp_availability": nk.get("pdp_availability", ""),
            "nykaa_page_broken": broken,
            "nykaa_title": nk.get("obs_title", ""), "d2c_title": d2c.get("title", ""),
            "title_seq_ratio": title_sim["seq_ratio"],
            "title_d2c_term_coverage": title_sim["d2c_term_coverage"],
            "title_plausible": title_sim["plausible"],
            "desc_seq_ratio": desc_sim["seq_ratio"], "desc_token_jaccard": desc_sim["token_jaccard"],
            "ingredients_coverage": ing_cov,
            "nykaa_extraction_source": nk.get("extraction_source", ""),
            "nykaa_image_count": nk.get("obs_image_count", ""),
            "d2c_image_count": d2c.get("image_count", ""),
            "image_count_flag": img_flag,
            "visual_best_similarity": visual_best, "visual_avg_similarity": visual_avg,
        })

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS)
        w.writeheader()
        w.writerows(out_rows)

    print(f"Diffed {len(out_rows)} Nykaa rows against {len(d2c_by_sku)} D2C reference SKUs.")
    if unmatched:
        uniq = sorted(set(unmatched))
        print(f"WARNING: {len(unmatched)} Nykaa rows ({len(uniq)} distinct nh_sku) had no D2C match "
              f"-- not in {d2c_path}. First few: {uniq[:10]}")
    print(f"-> {out_path}")

    import collections
    n_broken = sum(1 for r in out_rows if r["nykaa_page_broken"])
    if n_broken:
        print(f"NYKAA_PAGE_BROKEN: {n_broken}/{len(out_rows)} rows are a Nykaa 404/removed listing "
              f"or had no extractable content at all -- title/description/ingredients/image "
              f"comparisons are skipped for these (nothing to compare against), but they need a "
              f"manual look on Nykaa's side.")
    comparable = [r for r in out_rows if not r["nykaa_page_broken"]]
    print(f"image_count_flag mix (comparable rows only, n={len(comparable)}):",
          dict(collections.Counter(r["image_count_flag"] for r in comparable)))
    not_plausible = sum(1 for r in comparable if r["title_plausible"] is False)
    print(f"titles flagged not-plausible (eyeball these, not a hard fail): {not_plausible}/{len(comparable)}")


# ===========================================================================
# Selftest -- only what's genuinely Nykaa-specific. The shared pure
# functions (description_similarity, title_sanity, image_count_flag,
# visual_similarity, ingredients_coverage) already have their own selftest
# in qa_diff.py --selftest; no need to re-test them here.
# ===========================================================================
def selftest():
    checks = []

    checks.append(("is_nykaa_page_broken: page_not_found is broken",
                    is_nykaa_page_broken("page_not_found") is True))
    checks.append(("is_nykaa_page_broken: no_content is broken",
                    is_nykaa_page_broken("no_content") is True))
    checks.append(("is_nykaa_page_broken: unavailable (real page, out of stock) is NOT broken -- "
                    "there's still real content worth comparing",
                    is_nykaa_page_broken("unavailable") is False))
    checks.append(("is_nykaa_page_broken: available is NOT broken",
                    is_nykaa_page_broken("available") is False))

    ok = True
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print("SELFTEST", "PASSED" if ok else "FAILED")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--nykaa", default=NYKAA_DEFAULT)
    ap.add_argument("--d2c", default=D2C_DEFAULT)
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--visual", action="store_true",
                     help="also download images and compare them perceptually "
                          "(needs network + `pip install pillow`)")
    a = ap.parse_args()

    if a.selftest:
        selftest()
        sys.exit(0)

    run(a.nykaa, a.d2c, a.out, do_visual=a.visual)
