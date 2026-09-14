#!/usr/bin/env python3
"""
Amazon-vs-D2C diff engine -- Amazon's counterpart to qa_diff.py (Flipkart),
nykaa_qa_diff.py, and myntra_qa_diff.py. Imports the shared pure comparison
functions from qa_diff.py (description_similarity, title_sanity,
ingredients_coverage, shelf_life_match, image_count_flag, visual_similarity)
rather than re-implementing them, same reasoning as the other platforms: a
fix made there automatically applies to every platform and they never drift
apart on shared logic.

Inputs:
  observed_amazon_latest.csv    -- from amazon_pdp_scraper.py
  nathabit_reference_latest.csv -- from nathabit_pdp_scraper.py (the SAME
                                    D2C reference file every platform's diff
                                    uses -- keyed by nh_sku, shared across
                                    platforms, no separate D2C scrape needed)

Join key: nh_sku. NOTE: unlike every other platform, the Amazon worklist can
carry MORE THAN ONE row per nh_sku (confirmed 2026-09-14: 13 SKUs have 2+
simultaneously-Active ASINs in Anika's own master data, kept as separate
worklist rows by her explicit choice) -- so a single nh_sku can appear more
than once in the output here, each keyed by its own ASIN. This is expected,
not a bug; build_kam_review.py's doc_id format (nh_sku__amazon__asin) can
never collide between them.

What's DIFFERENT from Flipkart/Nykaa/Myntra, and why:
  - Amazon has no single structured "shelf life" field like Myntra's. When
    present at all, it's free text under a "Storage:" label inside the
    "Important information" block (see amazon_pdp_scraper.py's SHELF-LIFE
    FINDING) -- reuses qa_diff.py's shared shelf_life_match() unchanged,
    same as every other platform's free-text fallback path. Many products
    (non-perishables) genuinely have none at all, same situation as Nykaa,
    but confirmed (2026-09-14) some Amazon listings DO carry it, so this
    can produce real P1 rows where the value is present and differs.
  - "amazon_page_broken" covers pdp_availability in ("page_not_found",
    "no_content") -- see amazon_pdp_scraper.py's is_page_not_found()
    finding (confirmed against a deliberately bogus ASIN). "unavailable"
    (real page, genuinely out of stock) is NOT broken, same design as every
    other platform -- its title/description/ingredients/shelf-life are all
    still real, comparable data.
  - no_d2c_match is built in from day one here (unlike qa_diff.py/
    nykaa_qa_diff.py, where it was added after a real incident of SKUs with
    no D2C reference row silently vanishing from the sweep) -- see
    qa_diff.py's run() for the full reasoning. Same neutral framing: NOT
    assumed to mean "discontinued", since it could be a marketplace-only
    SKU, a stale D2C URL, or a D2C scrape gap.
  - Amazon's bot-check interstitial (see amazon_pdp_scraper.py) never
    reaches this file at all -- it's caught and retried at scrape time, so
    every row here reflects either real product data or a genuine
    page-not-found/no-content signal, never a transient block.

What each output column means: see qa_diff.py's module docstring for the
shared columns (title_*, desc_*, ingredients_coverage, shelf_life_status,
image_count_flag, visual_*) -- identical logic, applied to amazon_title/
amazon_image_count/etc instead of flipkart_title/flipkart_image_count.

Usage:
    python amazon_qa_diff.py --selftest
    python amazon_qa_diff.py --amazon observed_amazon_latest.csv --d2c nathabit_reference_latest.csv --out amazon_diff_latest.csv
    python amazon_qa_diff.py --visual   # also downloads + compares images, needs network + pillow
"""

import argparse
import csv
import os
import sys
import time

import qa_diff  # shared pure comparison functions live here

AMAZON_DEFAULT = "observed_amazon_latest.csv"
D2C_DEFAULT = "nathabit_reference_latest.csv"
OUT_DEFAULT = "amazon_diff_latest.csv"
VISUAL_CACHE_DIR = "qa_diff_image_cache"  # shared cache dir -- same D2C images get reused, no re-download

OUT_COLS = [
    "nh_sku", "asin", "pdp_availability", "amazon_page_broken", "no_d2c_match",
    "amazon_title", "d2c_title",
    "title_seq_ratio", "title_d2c_term_coverage", "title_plausible",
    "desc_seq_ratio", "desc_token_jaccard",
    "ingredients_coverage",
    "shelf_life_status", "d2c_shelf_life", "amazon_shelf_life",
    "amazon_extraction_source",
    "amazon_image_count", "d2c_image_count", "image_count_flag",
    "visual_best_similarity", "visual_avg_similarity",
]


def is_amazon_page_broken(pdp_availability):
    """True when there's nothing real to compare against the D2C reference:
    the page returned Amazon's "Page Not Found" static page
    (pdp_availability == "page_not_found") or extraction found no usable
    content at all (pdp_availability == "no_content"). Genuinely
    out-of-stock-but-real ("unavailable") is NOT broken -- that page still
    has real title/description/ingredients/shelf-life worth comparing."""
    return pdp_availability in ("page_not_found", "no_content")


# ===========================================================================
# Orchestration
# ===========================================================================
def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run(amazon_path, d2c_path, out_path, do_visual=False):
    if not os.path.exists(amazon_path):
        sys.exit(f"{amazon_path} not found -- run amazon_pdp_scraper.py first.")
    if not os.path.exists(d2c_path):
        sys.exit(f"{d2c_path} not found -- run nathabit_pdp_scraper.py first "
                  f"(the same D2C reference file every platform's diff uses).")

    amazon_rows = load_csv(amazon_path)
    d2c_rows = load_csv(d2c_path)
    d2c_by_sku = {r["nh_sku"]: r for r in d2c_rows if r.get("nh_sku")}
    print(f"Loaded {len(amazon_rows)} Amazon rows and {len(d2c_rows)} D2C reference rows.", flush=True)

    client = None
    if do_visual:
        import httpx
        client = httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)

        all_urls = []
        for az in amazon_rows:
            if is_amazon_page_broken(az.get("pdp_availability", "")):
                continue
            all_urls.extend(u for u in (az.get("obs_image_urls") or "").split("|") if u)
            d2c = d2c_by_sku.get(az.get("nh_sku", ""))
            if d2c:
                all_urls.extend(u for u in (d2c.get("image_urls") or "").split("|") if u)
        qa_diff._prefetch_images(all_urls, VISUAL_CACHE_DIR, client)

    unmatched = []
    out_rows = []
    t_start = time.time()
    n_total = len(amazon_rows)
    for i, az in enumerate(amazon_rows, 1):
        if i % 50 == 0 or i == n_total:
            print(f"  diffing row {i}/{n_total} -- {time.time() - t_start:.0f}s elapsed", flush=True)
        nh_sku = az.get("nh_sku", "")
        d2c = d2c_by_sku.get(nh_sku)
        if not d2c:
            # No D2C reference row for this SKU at all -- can't compare
            # anything. Deliberately NOT assumed to mean "discontinued": it
            # could just as easily be a marketplace-only SKU that was never
            # meant to be on nathabit.in, a D2C scrape that hasn't reached
            # it yet, or a genuinely stale URL. Flagged as its own tier so a
            # KAM checks the SKU codes master for that SKU's live status.
            unmatched.append(nh_sku)
            out_rows.append({
                "nh_sku": nh_sku, "asin": az.get("asin", ""),
                "pdp_availability": az.get("pdp_availability", ""),
                "amazon_page_broken": "", "no_d2c_match": True,
                "amazon_title": az.get("obs_title", ""), "d2c_title": "",
                "title_seq_ratio": "", "title_d2c_term_coverage": "", "title_plausible": "",
                "desc_seq_ratio": "", "desc_token_jaccard": "",
                "ingredients_coverage": "",
                "shelf_life_status": "no_d2c_match", "d2c_shelf_life": "", "amazon_shelf_life": "",
                "amazon_extraction_source": az.get("extraction_source", ""),
                "amazon_image_count": az.get("obs_image_count", ""), "d2c_image_count": "",
                "image_count_flag": "no_d2c_match",
                "visual_best_similarity": "", "visual_avg_similarity": "",
            })
            continue

        broken = is_amazon_page_broken(az.get("pdp_availability", ""))
        if broken:
            desc_sim = {"seq_ratio": "", "token_jaccard": ""}
            title_sim = {"seq_ratio": "", "d2c_term_coverage": "", "plausible": ""}
            ing_cov = ""
            shelf_status = "amazon_page_broken"
            az_shelf_life = ""
        else:
            az_shelf_life = az.get("obs_shelf_life", "")
            desc_sim = qa_diff.description_similarity(az.get("obs_description_text", ""), d2c.get("description", ""))
            title_sim = qa_diff.title_sanity(az.get("obs_title", ""), d2c.get("title", ""))
            ing_cov = qa_diff.ingredients_coverage(az.get("obs_description_text", ""), d2c.get("ingredients", ""))
            shelf_status = qa_diff.shelf_life_match(az.get("obs_description_text", ""),
                                                     d2c.get("shelf_life", ""),
                                                     az_shelf_life)
            ing_cov = ing_cov if ing_cov is not None else ""
        img_flag = ("amazon_page_broken" if broken
                    else qa_diff.image_count_flag(az.get("obs_image_count"), d2c.get("image_count")))

        visual_best = visual_avg = ""
        if do_visual and not broken:
            az_urls = [u for u in (az.get("obs_image_urls") or "").split("|") if u]
            d2c_urls = [u for u in (d2c.get("image_urls") or "").split("|") if u]
            best, avg = qa_diff.visual_similarity(az_urls, d2c_urls, VISUAL_CACHE_DIR, client)
            visual_best = best if best is not None else ""
            visual_avg = avg if avg is not None else ""

        out_rows.append({
            "nh_sku": nh_sku, "asin": az.get("asin", ""),
            "pdp_availability": az.get("pdp_availability", ""),
            "amazon_page_broken": broken, "no_d2c_match": False,
            "amazon_title": az.get("obs_title", ""), "d2c_title": d2c.get("title", ""),
            "title_seq_ratio": title_sim["seq_ratio"],
            "title_d2c_term_coverage": title_sim["d2c_term_coverage"],
            "title_plausible": title_sim["plausible"],
            "desc_seq_ratio": desc_sim["seq_ratio"], "desc_token_jaccard": desc_sim["token_jaccard"],
            "ingredients_coverage": ing_cov,
            "shelf_life_status": shelf_status, "d2c_shelf_life": d2c.get("shelf_life", ""),
            "amazon_shelf_life": az_shelf_life,
            "amazon_extraction_source": az.get("extraction_source", ""),
            "amazon_image_count": az.get("obs_image_count", ""),
            "d2c_image_count": d2c.get("image_count", ""),
            "image_count_flag": img_flag,
            "visual_best_similarity": visual_best, "visual_avg_similarity": visual_avg,
        })

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS)
        w.writeheader()
        w.writerows(out_rows)

    print(f"Diffed {len(out_rows)} Amazon rows against {len(d2c_by_sku)} D2C reference SKUs.")
    if unmatched:
        uniq = sorted(set(unmatched))
        print(f"NO_D2C_MATCH: {len(unmatched)} Amazon rows ({len(uniq)} distinct nh_sku) had no D2C match "
              f"-- not in {d2c_path}. Written to {out_path} with no_d2c_match=True (not assumed "
              f"discontinued -- could be marketplace-only, a stale D2C URL, or a D2C scrape gap; check "
              f"the SKU codes master for that SKU's live status). First few: {uniq[:10]}")
    print(f"-> {out_path}")

    import collections
    n_broken = sum(1 for r in out_rows if r["amazon_page_broken"])
    if n_broken:
        print(f"AMAZON_PAGE_BROKEN: {n_broken}/{len(out_rows)} rows are an Amazon delisted/removed "
              f"listing or had no extractable content at all -- title/description/ingredients/"
              f"shelf-life/image comparisons are skipped for these (nothing to compare against), "
              f"but they need a manual look on Amazon's side.")
    comparable = [r for r in out_rows if not r["amazon_page_broken"] and not r["no_d2c_match"]]
    print(f"image_count_flag mix (comparable rows only, n={len(comparable)}):",
          dict(collections.Counter(r["image_count_flag"] for r in comparable)))
    print("shelf_life_status mix (comparable rows only):",
          dict(collections.Counter(r["shelf_life_status"] for r in comparable)))
    not_plausible = sum(1 for r in comparable if r["title_plausible"] is False)
    print(f"titles flagged not-plausible (eyeball these, not a hard fail): {not_plausible}/{len(comparable)}")


# ===========================================================================
# Selftest -- only what's genuinely Amazon-specific. The shared pure
# functions already have their own selftest in qa_diff.py --selftest.
# ===========================================================================
def selftest():
    checks = []

    checks.append(("is_amazon_page_broken: page_not_found is broken",
                    is_amazon_page_broken("page_not_found") is True))
    checks.append(("is_amazon_page_broken: no_content is broken",
                    is_amazon_page_broken("no_content") is True))
    checks.append(("is_amazon_page_broken: unavailable (real page, out of stock) is NOT broken -- "
                    "there's still real content worth comparing",
                    is_amazon_page_broken("unavailable") is False))
    checks.append(("is_amazon_page_broken: available is NOT broken",
                    is_amazon_page_broken("available") is False))

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
    ap.add_argument("--amazon", default=AMAZON_DEFAULT)
    ap.add_argument("--d2c", default=D2C_DEFAULT)
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--visual", action="store_true",
                     help="also download images and compare them perceptually "
                          "(needs network + `pip install pillow`)")
    a = ap.parse_args()

    if a.selftest:
        selftest()
        sys.exit(0)

    run(a.amazon, a.d2c, a.out, do_visual=a.visual)
