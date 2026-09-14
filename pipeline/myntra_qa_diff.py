#!/usr/bin/env python3
"""
Myntra-vs-D2C diff engine -- Myntra's counterpart to qa_diff.py (Flipkart)
and nykaa_qa_diff.py. Imports the shared pure comparison functions from
qa_diff.py (description_similarity, title_sanity, ingredients_coverage,
shelf_life_match, image_count_flag, visual_similarity) rather than
re-implementing them, same reasoning as nykaa_qa_diff.py: a fix made there
automatically applies to every platform and they never drift apart on
shared logic.

Inputs:
  observed_myntra_latest.csv    -- from myntra_pdp_scraper.py
  nathabit_reference_latest.csv -- from nathabit_pdp_scraper.py (the SAME
                                    D2C reference file every platform's diff
                                    uses -- keyed by nh_sku, shared across
                                    platforms, no separate D2C scrape needed)

Join key: nh_sku.

What's DIFFERENT from Flipkart/Nykaa, and why:
  - UNLIKE Nykaa, Myntra DOES get a real P1 Shelf Life Mismatch tier --
    confirmed (2026-09-14, both real sample products) that Myntra's
    articleAttributes carries real, populated "Minimum Shelf Life in
    Months" and "Total Shelf Life in Months" fields. Per Anika's explicit
    instruction, only the "maximum" one (Total Shelf Life in Months,
    reformatted by the scraper to e.g. "6 Months") is compared against
    D2C's shelf life -- the same semantics as Flipkart's "Maximum Shelf
    Life" spec field, reusing qa_diff.py's shared shelf_life_match()
    unchanged.
  - "myntra_page_broken" covers pdp_availability in ("page_not_found",
    "no_content") -- see myntra_pdp_scraper.py's is_page_not_found()
    finding (confirmed against a deliberately bogus style ID, since none of
    the real SKUs tried turned out to be actually dead on Myntra itself).
    "unavailable" (real page, genuinely out of stock) is NOT broken, same
    design as every other platform -- its title/description/ingredients/
    shelf-life are all still real, comparable data.
  - no_d2c_match is built in from day one here (unlike qa_diff.py/
    nykaa_qa_diff.py, where it was added after a real incident of SKUs with
    no D2C reference row silently vanishing from the sweep) -- see
    qa_diff.py's run() for the full reasoning. Same neutral framing: NOT
    assumed to mean "discontinued", since it could be a marketplace-only
    SKU, a stale D2C URL, or a D2C scrape gap.

What each output column means: see qa_diff.py's module docstring for the
shared columns (title_*, desc_*, ingredients_coverage, shelf_life_status,
image_count_flag, visual_*) -- identical logic, applied to myntra_title/
myntra_image_count/etc instead of flipkart_title/flipkart_image_count.

Usage:
    python myntra_qa_diff.py --selftest
    python myntra_qa_diff.py --myntra observed_myntra_latest.csv --d2c nathabit_reference_latest.csv --out myntra_diff_latest.csv
    python myntra_qa_diff.py --visual   # also downloads + compares images, needs network + pillow
"""

import argparse
import csv
import os
import sys
import time

import qa_diff  # shared pure comparison functions live here

MYNTRA_DEFAULT = "observed_myntra_latest.csv"
D2C_DEFAULT = "nathabit_reference_latest.csv"
OUT_DEFAULT = "myntra_diff_latest.csv"
VISUAL_CACHE_DIR = "qa_diff_image_cache"  # shared cache dir -- same D2C images get reused, no re-download

OUT_COLS = [
    "nh_sku", "style_id", "pdp_availability", "myntra_page_broken", "no_d2c_match",
    "myntra_title", "d2c_title",
    "title_seq_ratio", "title_d2c_term_coverage", "title_plausible",
    "desc_seq_ratio", "desc_token_jaccard",
    "ingredients_coverage",
    "shelf_life_status", "d2c_shelf_life", "myntra_max_shelf_life",
    "myntra_extraction_source",
    "myntra_image_count", "d2c_image_count", "image_count_flag",
    "visual_best_similarity", "visual_avg_similarity",
]


def is_myntra_page_broken(pdp_availability):
    """True when there's nothing real to compare against the D2C reference:
    pdpData was completely missing (pdp_availability == "page_not_found") or
    extraction found no usable content at all (pdp_availability ==
    "no_content"). Genuinely out-of-stock-but-real ("unavailable") is NOT
    broken -- that page still has real title/description/ingredients/
    shelf-life worth comparing."""
    return pdp_availability in ("page_not_found", "no_content")


# ===========================================================================
# Orchestration
# ===========================================================================
def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run(myntra_path, d2c_path, out_path, do_visual=False):
    if not os.path.exists(myntra_path):
        sys.exit(f"{myntra_path} not found -- run myntra_pdp_scraper.py first.")
    if not os.path.exists(d2c_path):
        sys.exit(f"{d2c_path} not found -- run nathabit_pdp_scraper.py first "
                  f"(the same D2C reference file every platform's diff uses).")

    myntra_rows = load_csv(myntra_path)
    d2c_rows = load_csv(d2c_path)
    d2c_by_sku = {r["nh_sku"]: r for r in d2c_rows if r.get("nh_sku")}
    print(f"Loaded {len(myntra_rows)} Myntra rows and {len(d2c_rows)} D2C reference rows.", flush=True)

    client = None
    if do_visual:
        import httpx
        client = httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)

        all_urls = []
        for mx in myntra_rows:
            if is_myntra_page_broken(mx.get("pdp_availability", "")):
                continue
            all_urls.extend(u for u in (mx.get("obs_image_urls") or "").split("|") if u)
            d2c = d2c_by_sku.get(mx.get("nh_sku", ""))
            if d2c:
                all_urls.extend(u for u in (d2c.get("image_urls") or "").split("|") if u)
        qa_diff._prefetch_images(all_urls, VISUAL_CACHE_DIR, client)

    unmatched = []
    out_rows = []
    t_start = time.time()
    n_total = len(myntra_rows)
    for i, mx in enumerate(myntra_rows, 1):
        if i % 50 == 0 or i == n_total:
            print(f"  diffing row {i}/{n_total} -- {time.time() - t_start:.0f}s elapsed", flush=True)
        nh_sku = mx.get("nh_sku", "")
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
                "nh_sku": nh_sku, "style_id": mx.get("style_id", ""),
                "pdp_availability": mx.get("pdp_availability", ""),
                "myntra_page_broken": "", "no_d2c_match": True,
                "myntra_title": mx.get("obs_title", ""), "d2c_title": "",
                "title_seq_ratio": "", "title_d2c_term_coverage": "", "title_plausible": "",
                "desc_seq_ratio": "", "desc_token_jaccard": "",
                "ingredients_coverage": "",
                "shelf_life_status": "no_d2c_match", "d2c_shelf_life": "", "myntra_max_shelf_life": "",
                "myntra_extraction_source": mx.get("extraction_source", ""),
                "myntra_image_count": mx.get("obs_image_count", ""), "d2c_image_count": "",
                "image_count_flag": "no_d2c_match",
                "visual_best_similarity": "", "visual_avg_similarity": "",
            })
            continue

        broken = is_myntra_page_broken(mx.get("pdp_availability", ""))
        if broken:
            desc_sim = {"seq_ratio": "", "token_jaccard": ""}
            title_sim = {"seq_ratio": "", "d2c_term_coverage": "", "plausible": ""}
            ing_cov = ""
            shelf_status = "myntra_page_broken"
            mx_max_shelf_life = ""
        else:
            mx_max_shelf_life = mx.get("obs_max_shelf_life", "")
            desc_sim = qa_diff.description_similarity(mx.get("obs_description_text", ""), d2c.get("description", ""))
            title_sim = qa_diff.title_sanity(mx.get("obs_title", ""), d2c.get("title", ""))
            ing_cov = qa_diff.ingredients_coverage(mx.get("obs_description_text", ""), d2c.get("ingredients", ""))
            shelf_status = qa_diff.shelf_life_match(mx.get("obs_description_text", ""),
                                                     d2c.get("shelf_life", ""),
                                                     mx_max_shelf_life)
            ing_cov = ing_cov if ing_cov is not None else ""
        img_flag = ("myntra_page_broken" if broken
                    else qa_diff.image_count_flag(mx.get("obs_image_count"), d2c.get("image_count")))

        visual_best = visual_avg = ""
        if do_visual and not broken:
            mx_urls = [u for u in (mx.get("obs_image_urls") or "").split("|") if u]
            d2c_urls = [u for u in (d2c.get("image_urls") or "").split("|") if u]
            best, avg = qa_diff.visual_similarity(mx_urls, d2c_urls, VISUAL_CACHE_DIR, client)
            visual_best = best if best is not None else ""
            visual_avg = avg if avg is not None else ""

        out_rows.append({
            "nh_sku": nh_sku, "style_id": mx.get("style_id", ""),
            "pdp_availability": mx.get("pdp_availability", ""),
            "myntra_page_broken": broken, "no_d2c_match": False,
            "myntra_title": mx.get("obs_title", ""), "d2c_title": d2c.get("title", ""),
            "title_seq_ratio": title_sim["seq_ratio"],
            "title_d2c_term_coverage": title_sim["d2c_term_coverage"],
            "title_plausible": title_sim["plausible"],
            "desc_seq_ratio": desc_sim["seq_ratio"], "desc_token_jaccard": desc_sim["token_jaccard"],
            "ingredients_coverage": ing_cov,
            "shelf_life_status": shelf_status, "d2c_shelf_life": d2c.get("shelf_life", ""),
            "myntra_max_shelf_life": mx_max_shelf_life,
            "myntra_extraction_source": mx.get("extraction_source", ""),
            "myntra_image_count": mx.get("obs_image_count", ""),
            "d2c_image_count": d2c.get("image_count", ""),
            "image_count_flag": img_flag,
            "visual_best_similarity": visual_best, "visual_avg_similarity": visual_avg,
        })

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS)
        w.writeheader()
        w.writerows(out_rows)

    print(f"Diffed {len(out_rows)} Myntra rows against {len(d2c_by_sku)} D2C reference SKUs.")
    if unmatched:
        uniq = sorted(set(unmatched))
        print(f"NO_D2C_MATCH: {len(unmatched)} Myntra rows ({len(uniq)} distinct nh_sku) had no D2C match "
              f"-- not in {d2c_path}. Written to {out_path} with no_d2c_match=True (not assumed "
              f"discontinued -- could be marketplace-only, a stale D2C URL, or a D2C scrape gap; check "
              f"the SKU codes master for that SKU's live status). First few: {uniq[:10]}")
    print(f"-> {out_path}")

    import collections
    n_broken = sum(1 for r in out_rows if r["myntra_page_broken"])
    if n_broken:
        print(f"MYNTRA_PAGE_BROKEN: {n_broken}/{len(out_rows)} rows are a Myntra delisted/removed "
              f"listing or had no extractable content at all -- title/description/ingredients/"
              f"shelf-life/image comparisons are skipped for these (nothing to compare against), "
              f"but they need a manual look on Myntra's side.")
    comparable = [r for r in out_rows if not r["myntra_page_broken"] and not r["no_d2c_match"]]
    print(f"image_count_flag mix (comparable rows only, n={len(comparable)}):",
          dict(collections.Counter(r["image_count_flag"] for r in comparable)))
    print("shelf_life_status mix (comparable rows only):",
          dict(collections.Counter(r["shelf_life_status"] for r in comparable)))
    not_plausible = sum(1 for r in comparable if r["title_plausible"] is False)
    print(f"titles flagged not-plausible (eyeball these, not a hard fail): {not_plausible}/{len(comparable)}")


# ===========================================================================
# Selftest -- only what's genuinely Myntra-specific. The shared pure
# functions already have their own selftest in qa_diff.py --selftest.
# ===========================================================================
def selftest():
    checks = []

    checks.append(("is_myntra_page_broken: page_not_found is broken",
                    is_myntra_page_broken("page_not_found") is True))
    checks.append(("is_myntra_page_broken: no_content is broken",
                    is_myntra_page_broken("no_content") is True))
    checks.append(("is_myntra_page_broken: unavailable (real page, out of stock) is NOT broken -- "
                    "there's still real content worth comparing",
                    is_myntra_page_broken("unavailable") is False))
    checks.append(("is_myntra_page_broken: available is NOT broken",
                    is_myntra_page_broken("available") is False))

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
    ap.add_argument("--myntra", default=MYNTRA_DEFAULT)
    ap.add_argument("--d2c", default=D2C_DEFAULT)
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--visual", action="store_true",
                     help="also download images and compare them perceptually "
                          "(needs network + `pip install pillow`)")
    a = ap.parse_args()

    if a.selftest:
        selftest()
        sys.exit(0)

    run(a.myntra, a.d2c, a.out, do_visual=a.visual)
