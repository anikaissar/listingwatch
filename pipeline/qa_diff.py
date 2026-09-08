#!/usr/bin/env python3
"""
Flipkart-vs-D2C diff engine -- the actual QA comparison, per your brief:
  - images: does the Flipkart gallery look like the nathabit.in gallery?
  - text: is the description/ingredients/shelf-life the same or "close enough"?
  - title: does it make sense for the SKU (not required to match exactly --
    platforms phrase titles differently)?

Inputs (both produced by the two PDP scrapers -- run those first):
  observed_flipkart_v2.csv   -- from flipkart_pdp_scraper.py (needs the
                                 obs_image_urls column -- the original
                                 observed_flipkart.csv predates that field
                                 and can't feed --visual)
  nathabit_reference.csv     -- from nathabit_pdp_scraper.py, scraping your
                                 nh_sku -> nathabit.in URL mapping

Join key: nh_sku. This is many-to-one on purpose -- you confirmed some
nh_skus have multiple deliberate Flipkart listings (different FSNs) for
category-reach; each one is diffed independently against the same single
D2C reference row for that nh_sku.

What each row of the output means:
  flipkart_page_broken
      True when Flipkart served its generic placeholder page ("X Store
      Online - Buy X Online at Best Price in India") instead of real product
      data -- confirmed to be the case for every row where extraction_source
      is "meta" in this dataset, not an occasional glitch. All the
      title/description/ingredients/shelf-life/image columns are left blank
      for these rows (nothing real to compare), so they don't show up as
      false "mismatches" -- but they need a manual look on Flipkart's side:
      that FSN may be dead, soft-blocked, or otherwise broken.
  title_seq_ratio / title_d2c_term_coverage / title_plausible
      Not an exact-match test (titles are expected to differ by platform).
      term_coverage = fraction of the D2C title's meaningful words that also
      show up in the Flipkart title. plausible=False is a flag to eyeball,
      not a hard failure -- e.g. a listing titled entirely around a bundle
      name may legitimately score low here.
  desc_seq_ratio / desc_token_jaccard
      Two fuzzy-similarity readings of the description text (Flipkart's
      "Flipkart.com: Buy ... from Flipkart.com." boilerplate is stripped
      first so it doesn't drag every score down uniformly). seq_ratio is
      stricter (character-level, order-sensitive); token_jaccard is more
      forgiving (bag-of-words overlap) -- reworded-but-same-substance copy
      typically shows low seq_ratio but decent token_jaccard. Read both.
  ingredients_coverage
      Fraction of the D2C ingredient-list terms found verbatim inside the
      Flipkart description text (Flipkart has no separate ingredients
      field, so this is the closest available check). Blank if the D2C
      side has no ingredient list for that SKU (e.g. hard goods).
  shelf_life_status
      match / mismatch / not_found / unparseable / d2c_missing -- compares
      the D2C "Use within N months of Mfg Date" duration against Flipkart's
      "Maximum Shelf Life" field (flipkart_max_shelf_life, scraped from the
      on-page Specifications grid -- an authoritative structured value, not
      a free-text guess). Falls back to hunting for a duration inside the
      Flipkart description only on older scrapes that predate this field
      (flipkart_max_shelf_life blank) -- a much weaker signal, kept for
      backward compatibility only.
  image_count_flag
      match / minor_mismatch / large_mismatch / flipkart_no_images /
      d2c_no_images -- compares obs_image_count vs the D2C gallery count.
      NOTE: rows where extraction_source=meta on the Flipkart side have an
      unreliable count (meta tags only ever carry 1 image) -- that caveat
      from the original QA sweep still applies here.
  visual_best_similarity / visual_avg_similarity  (only with --visual)
      Perceptual-hash comparison of actual image content, downloaded from
      both obs_image_urls and the D2C image_urls. 1.0 = a near-identical
      image exists on both sides; near 0 = nothing matches. Needs network
      (both flipkart's and nathabit's image CDNs) -- run this on your
      machine, and `pip install pillow` first.

Usage:
    python qa_diff.py --selftest                 # validates the pure comparison functions, no network
    python qa_diff.py                             # text-only diff -> qa_diff_flipkart_vs_d2c.csv
    python qa_diff.py --visual                     # also downloads + compares images (slower, needs network + pillow)
"""

import argparse
import concurrent.futures as cf
import csv
import difflib
import hashlib
import os
import re
import sys
import time

FLIPKART_DEFAULT = "observed_flipkart_v2.csv"
D2C_DEFAULT = "nathabit_reference.csv"
OUT_DEFAULT = "qa_diff_flipkart_vs_d2c.csv"
VISUAL_CACHE_DIR = "qa_diff_image_cache"

OUT_COLS = [
    "nh_sku", "fsn", "pdp_availability", "flipkart_page_broken",
    "flipkart_title", "d2c_title",
    "title_seq_ratio", "title_d2c_term_coverage", "title_plausible",
    "desc_seq_ratio", "desc_token_jaccard",
    "ingredients_coverage",
    "shelf_life_status", "d2c_shelf_life", "flipkart_max_shelf_life",
    "flipkart_extraction_source",
    "flipkart_image_count", "d2c_image_count", "image_count_flag",
    "visual_best_similarity", "visual_avg_similarity",
]

# Flipkart serves this exact generic template -- not a real product title --
# for a subset of listings where our scraper fell back to meta tags. Every
# meta-source row observed so far has this identical title, meaning those
# rows carry NO usable title/description/image data, not just "less
# reliable" data. Flagged separately so it doesn't masquerade as a normal
# title/description mismatch in the comparison columns.
_BROKEN_FLIPKART_TITLE = "x store online"

STOPWORDS = {
    "the", "a", "an", "for", "with", "and", "of", "to", "in", "on", "by",
    "nat", "habit", "nathabit", "pack", "ml", "g", "gm", "x",
}


# ===========================================================================
# Pure comparison functions -- unit-testable without any network (selftest)
# ===========================================================================
def normalize_text(t):
    t = (t or "").lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


def strip_flipkart_boilerplate(desc):
    """Flipkart descriptions are typically prefixed with
    'Flipkart.com: Buy <title> for Rs. <price> from Flipkart.com.' --
    strip it so it doesn't inflate/deflate similarity uniformly."""
    m = re.match(r"^\s*flipkart\.com\s*:\s*buy\s+.*?\bfrom\s+flipkart\.com\.?\s*",
                 desc or "", re.I | re.S)
    return desc[m.end():] if m else (desc or "")


def description_similarity(fk_desc, d2c_desc):
    a = normalize_text(strip_flipkart_boilerplate(fk_desc))
    b = normalize_text(d2c_desc)
    seq_ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    jaccard = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    return {"seq_ratio": round(seq_ratio, 3), "token_jaccard": round(jaccard, 3)}


def ingredients_coverage(fk_desc, ingredients_text):
    """None (not '') means 'not applicable' -- D2C side has no ingredient
    list for this SKU (e.g. a comb), so there's nothing to check."""
    if not (ingredients_text or "").strip():
        return None
    fk_norm = normalize_text(fk_desc)
    terms = [normalize_text(t) for t in ingredients_text.split(",")]
    terms = [t for t in terms if t]
    if not terms:
        return None
    found = sum(1 for t in terms if t in fk_norm)
    return round(found / len(terms), 3)


_DURATION_RE = re.compile(r"(\d+)\s*(day|days|month|months|year|years)", re.I)

# Calendar-approximate day-equivalents, used only to tell whether two
# durations expressed in *different* units are the same claim (e.g. D2C's
# "2 months" vs Flipkart's "60 Days" -- confirmed a real false-positive
# pattern in production: 7 of an initial 82 "mismatch" rows were actually
# this, not a real content discrepancy). Not calendar-exact (months vary
# 28-31 days) -- fine here, since nobody stating "9 months" shelf life means
# a specific day count either; this is a fuzzy "close enough" comparison by
# design, same as the rest of this project's text-similarity checks.
_UNIT_DAYS = {"day": 1, "month": 30, "year": 365}


def _duration_days(num_str, unit):
    return int(num_str) * _UNIT_DAYS[unit.lower().rstrip("s")]

# Flipkart boilerplate ("30 Day Replacement Guarantee", "10 Day Return Policy",
# "1 Year Warranty" on unrelated goods) reads as a duration but has nothing to
# do with product shelf life -- strip it out before hunting for a real one.
# Confirmed necessary: this boilerplate appears in ~69% of real Flipkart
# descriptions, and without stripping it, shelf_life_match returns "mismatch"
# for nearly everything regardless of the actual text.
_POLICY_BOILERPLATE_RE = re.compile(
    r"\d+\s*(?:day|days|month|months|year|years)\s*"
    r"(?:replacement|return|returns?\s*policy|warranty|guarantee|exchange)",
    re.I,
)

# Also unrelated to shelf life: age-restriction language ("children above 5
# years", "not for kids under 3 years", "5 years and above"), which reads as
# a duration but describes who the product is safe for, not how long it lasts.
_AGE_BOILERPLATE_RE = re.compile(
    r"(?:above|under|over|below)\s+\d+\s*(?:day|days|month|months|year|years)"
    r"|\d+\s*(?:day|days|month|months|year|years)\s*(?:of age|old|and above|and up|or older)",
    re.I,
)


def _strip_policy_boilerplate(text):
    text = _POLICY_BOILERPLATE_RE.sub("", text or "")
    return _AGE_BOILERPLATE_RE.sub("", text)


def shelf_life_match(fk_desc, shelf_life_text, fk_max_shelf_life=""):
    """Compares D2C's stated shelf life against Flipkart's.

    Flipkart has no free-text "shelf life" field on its PDP -- it has a
    structured "Maximum Shelf Life" row in the on-page Specifications grid
    (scraped into obs_max_shelf_life / fk_max_shelf_life here), which is a
    direct, authoritative duration and is checked first when present. Older
    scraper runs won't have that column at all (fk_max_shelf_life=""), so
    this falls back to hunting for a duration inside the free-text
    description -- a much weaker signal (a duration mentioned in passing,
    e.g. "no side effects reported over 6 months of use", would otherwise
    read as a real shelf-life claim), kept only for backward compatibility
    with data scraped before the Specifications extraction existed.

    The structured-field comparison is unit-aware ("2 months" == "60 Days"),
    since D2C and Flipkart don't always phrase the same duration in the same
    unit. The free-text fallback path below is not -- it's a legacy path
    kept only for old data, not worth the same investment.
    """
    if not (shelf_life_text or "").strip():
        return "d2c_missing"
    m = _DURATION_RE.search(shelf_life_text)
    if not m:
        return "unparseable"
    num, unit = m.group(1), m.group(2).lower().rstrip("s")

    if (fk_max_shelf_life or "").strip():
        fk_m = _DURATION_RE.search(fk_max_shelf_life)
        if not fk_m:
            return "unparseable"
        fk_num, fk_unit = fk_m.group(1), fk_m.group(2).lower().rstrip("s")
        # Compare by calendar-approximate days, not the literal (number, unit)
        # pair -- "2 months" and "60 Days" are the same claim, and comparing
        # the raw tuple flagged that as a mismatch in production.
        return "match" if _duration_days(num, unit) == _duration_days(fk_num, fk_unit) else "mismatch"

    fk_clean = _strip_policy_boilerplate(fk_desc)
    if re.search(rf"\b{re.escape(num)}\s*{unit}s?\b", fk_clean, re.I):
        return "match"
    if _DURATION_RE.search(fk_clean):
        return "mismatch"
    return "not_found"


def title_sanity(fk_title, d2c_title):
    a, b = normalize_text(fk_title), normalize_text(d2c_title)
    seq_ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ta = set(a.split()) - STOPWORDS
    tb = set(b.split()) - STOPWORDS
    coverage = (len(ta & tb) / len(tb)) if tb else 0.0
    plausible = coverage >= 0.3 or seq_ratio >= 0.3
    return {"seq_ratio": round(seq_ratio, 3), "d2c_term_coverage": round(coverage, 3),
            "plausible": plausible}


def is_flipkart_page_broken(fk_title):
    return _BROKEN_FLIPKART_TITLE in normalize_text(fk_title)


def image_count_flag(fk_count, d2c_count):
    fk_count, d2c_count = int(fk_count or 0), int(d2c_count or 0)
    if d2c_count == 0:
        return "d2c_no_images"
    if fk_count == 0:
        return "flipkart_no_images"
    diff = abs(fk_count - d2c_count)
    if diff >= 3:
        return "large_mismatch"
    if diff >= 1:
        return "minor_mismatch"
    return "match"


# ===========================================================================
# Visual similarity (optional, --visual) -- needs network + pillow
# ===========================================================================
def _cache_path(cache_dir, url):
    ext = os.path.splitext(url.split("?")[0])[1] or ".jpg"
    name = hashlib.md5(url.encode("utf-8")).hexdigest() + ext
    return os.path.join(cache_dir, name)


def _download(url, cache_dir, client):
    path = _cache_path(cache_dir, url)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    try:
        r = client.get(url, timeout=15.0)
        if r.status_code == 200 and r.content:
            os.makedirs(cache_dir, exist_ok=True)
            with open(path, "wb") as f:
                f.write(r.content)
            return path
    except Exception:  # noqa: BLE001
        pass
    return None


def _prefetch_images(urls, cache_dir, client, max_workers=16):
    """Download every distinct image URL up front, in parallel, with progress
    printed as it goes. This is the fix for --visual appearing to 'hang': the
    row-by-row loop used to download images one at a time, sequentially, with
    zero output until the entire run finished -- for ~600 rows x several
    images each, over two different CDNs, that's easily 20-60+ minutes of
    total silence even though it's working fine. Prefetching everything
    concurrently is both much faster and gives visible progress; the main
    loop below then just reads from this on-disk cache (near-instant, no
    network) and can print per-row progress too."""
    urls = sorted(set(u for u in urls if u))
    total = len(urls)
    if total == 0:
        return
    print(f"Downloading {total} unique product images for visual comparison "
          f"({max_workers} at a time)...", flush=True)
    start = time.time()
    done = ok = 0
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_download, u, cache_dir, client) for u in urls]
        for fut in cf.as_completed(futures):
            done += 1
            if fut.result():
                ok += 1
            if done % 25 == 0 or done == total:
                print(f"  images: {done}/{total} ({ok} ok) -- {time.time() - start:.0f}s elapsed",
                      flush=True)
    print(f"Image download done: {ok}/{total} fetched in {time.time() - start:.0f}s.", flush=True)


def _autocrop_borders(im, tol=6, max_iters=6):
    """Trim solid-color letterbox/padding borders before hashing.

    Confirmed necessary (not theoretical): marketplaces commonly pad product
    photos to a required aspect ratio with plain white/gray bars, while a
    brand's own D2C photo of the exact same shot usually has no such padding
    -- so the two images differ only by an added border, but a plain resize
    to a fixed 9x8 grid squishes them by different amounts and drags the
    hash apart. Tested on a synthetic pair built to model this exact case
    (same photo; one copy letterboxed onto a square canvas): without this
    crop the dHash similarity was 0.594 (barely above the 0.5 floor for two
    *unrelated* images) even though it's the same picture; with it, 1.0.

    Runs iteratively (not just once) because a photo can have more than one
    nested border of different shades (e.g. the brand's own light background
    PLUS a marketplace's added pure-white bars) -- one pass only strips the
    outermost layer. `tol` absorbs JPEG/compression noise around the edges
    so it doesn't stop after a fraction of a pixel's difference.
    """
    from PIL import Image, ImageChops
    for _ in range(max_iters):
        bg = im.getpixel((0, 0))
        bg_im = Image.new(im.mode, im.size, bg)
        diff = ImageChops.difference(im, bg_im).point(lambda p: 255 if p > tol else 0)
        bbox = diff.getbbox()
        if not bbox or bbox == (0, 0, im.size[0], im.size[1]):
            break
        im = im.crop(bbox)
    return im


def _phash(path):
    """Difference hash (dHash): trim any letterbox padding (see
    _autocrop_borders), resize to 9x8 grayscale, compare each pixel to its
    right neighbor -> a 64-bit fingerprint. Deliberately dependency-free
    (Pillow only) rather than using the `imagehash` package -- that package
    pulls in PyWavelets, which needs to compile from source and can fail on
    newer/less common Python builds (confirmed: this failed on a real
    Windows Python 3.15 setup with 'metadata-generation-failed' building
    PyWavelets). dHash is simpler, has no such dependency, and is the
    standard technique for this exact "does a similar photo exist on the
    other site" comparison -- robust to resizing and re-compression, which
    is exactly what happens when the same product photo gets re-uploaded to
    two different platforms."""
    from PIL import Image
    try:
        with Image.open(path) as im:
            im = im.convert("L")
            im = _autocrop_borders(im)
            im = im.resize((9, 8), Image.LANCZOS)
            px = list(im.getdata())
            bits = 0
            for row in range(8):
                base = row * 9
                for col in range(8):
                    bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
            return bits
    except Exception:  # noqa: BLE001
        return None


def _hamming(a, b):
    return bin(a ^ b).count("1")


def visual_similarity(fk_urls, d2c_urls, cache_dir, client):
    """Returns (best_similarity, avg_similarity) in [0,1], or (None, None) if
    either side has no usable images. best = closest single image pair found
    (a strong 'yes, this photo is shared/near-identical' signal); avg = mean
    of each Flipkart image's best match (a rough 'whole gallery overlaps'
    signal). Similarity = 1 - (Hamming distance / 64 bits)."""
    if not fk_urls or not d2c_urls:
        return None, None
    fk_hashes = [h for h in (_phash(_download(u, cache_dir, client) or "") for u in fk_urls) if h is not None]
    d2c_hashes = [h for h in (_phash(_download(u, cache_dir, client) or "") for u in d2c_urls) if h is not None]
    if not fk_hashes or not d2c_hashes:
        return None, None
    best_per_fk = []
    for fh in fk_hashes:
        dist = min(_hamming(fh, dh) for dh in d2c_hashes)
        best_per_fk.append(1 - dist / 64)
    return round(max(best_per_fk), 3), round(sum(best_per_fk) / len(best_per_fk), 3)


# ===========================================================================
# Orchestration
# ===========================================================================
def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run(flipkart_path, d2c_path, out_path, do_visual=False):
    if not os.path.exists(flipkart_path):
        sys.exit(f"{flipkart_path} not found -- run flipkart_pdp_scraper.py first.")
    if not os.path.exists(d2c_path):
        sys.exit(f"{d2c_path} not found -- run nathabit_pdp_scraper.py first.")

    flipkart_rows = load_csv(flipkart_path)
    d2c_rows = load_csv(d2c_path)
    d2c_by_sku = {r["nh_sku"]: r for r in d2c_rows if r.get("nh_sku")}
    print(f"Loaded {len(flipkart_rows)} Flipkart rows and {len(d2c_rows)} D2C reference rows.", flush=True)

    client = None
    if do_visual:
        import httpx
        client = httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)

        # Download every image this run will need, up front and in parallel,
        # instead of one-at-a-time inside the row loop below (see
        # _prefetch_images' docstring for why -- that's what made --visual
        # look frozen).
        all_urls = []
        for fk in flipkart_rows:
            if is_flipkart_page_broken(fk.get("obs_title", "")):
                continue
            all_urls.extend(u for u in (fk.get("obs_image_urls") or "").split("|") if u)
            d2c = d2c_by_sku.get(fk.get("nh_sku", ""))
            if d2c:
                all_urls.extend(u for u in (d2c.get("image_urls") or "").split("|") if u)
        _prefetch_images(all_urls, VISUAL_CACHE_DIR, client)

    unmatched = []
    out_rows = []
    t_start = time.time()
    n_total = len(flipkart_rows)
    for i, fk in enumerate(flipkart_rows, 1):
        if i % 50 == 0 or i == n_total:
            print(f"  diffing row {i}/{n_total} -- {time.time() - t_start:.0f}s elapsed", flush=True)
        nh_sku = fk.get("nh_sku", "")
        d2c = d2c_by_sku.get(nh_sku)
        if not d2c:
            unmatched.append(nh_sku)
            continue

        broken = is_flipkart_page_broken(fk.get("obs_title", ""))
        if broken:
            # Flipkart served a generic placeholder page, not real product
            # data -- title/description/image comparisons here would just be
            # comparing garbage to the D2C reference. Skip them rather than
            # report a misleading "mismatch"; this SKU needs a re-check on
            # Flipkart's side (broken listing, soft-block, or dead FSN), not
            # a content fix.
            desc_sim = {"seq_ratio": "", "token_jaccard": ""}
            title_sim = {"seq_ratio": "", "d2c_term_coverage": "", "plausible": ""}
            ing_cov = ""
            shelf_status = "flipkart_page_broken"
            img_flag = "flipkart_page_broken"
            fk_max_shelf_life = ""
        else:
            fk_max_shelf_life = fk.get("obs_max_shelf_life", "")
            desc_sim = description_similarity(fk.get("obs_description_text", ""), d2c.get("description", ""))
            title_sim = title_sanity(fk.get("obs_title", ""), d2c.get("title", ""))
            ing_cov = ingredients_coverage(fk.get("obs_description_text", ""), d2c.get("ingredients", ""))
            shelf_status = shelf_life_match(fk.get("obs_description_text", ""),
                                             d2c.get("shelf_life", ""),
                                             fk.get("obs_max_shelf_life", ""))
            img_flag = image_count_flag(fk.get("obs_image_count"), d2c.get("image_count"))
            ing_cov = ing_cov if ing_cov is not None else ""

        visual_best = visual_avg = ""
        if do_visual and not broken:
            fk_urls = [u for u in (fk.get("obs_image_urls") or "").split("|") if u]
            d2c_urls = [u for u in (d2c.get("image_urls") or "").split("|") if u]
            best, avg = visual_similarity(fk_urls, d2c_urls, VISUAL_CACHE_DIR, client)
            visual_best = best if best is not None else ""
            visual_avg = avg if avg is not None else ""

        out_rows.append({
            "nh_sku": nh_sku, "fsn": fk.get("fsn", ""),
            "pdp_availability": fk.get("pdp_availability", ""),
            "flipkart_page_broken": broken,
            "flipkart_title": fk.get("obs_title", ""), "d2c_title": d2c.get("title", ""),
            "title_seq_ratio": title_sim["seq_ratio"],
            "title_d2c_term_coverage": title_sim["d2c_term_coverage"],
            "title_plausible": title_sim["plausible"],
            "desc_seq_ratio": desc_sim["seq_ratio"], "desc_token_jaccard": desc_sim["token_jaccard"],
            "ingredients_coverage": ing_cov,
            "shelf_life_status": shelf_status, "d2c_shelf_life": d2c.get("shelf_life", ""),
            "flipkart_max_shelf_life": fk_max_shelf_life,
            "flipkart_extraction_source": fk.get("extraction_source", ""),
            "flipkart_image_count": fk.get("obs_image_count", ""),
            "d2c_image_count": d2c.get("image_count", ""),
            "image_count_flag": img_flag,
            "visual_best_similarity": visual_best, "visual_avg_similarity": visual_avg,
        })

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS)
        w.writeheader()
        w.writerows(out_rows)

    print(f"Diffed {len(out_rows)} Flipkart rows against {len(d2c_by_sku)} D2C reference SKUs.")
    if unmatched:
        uniq = sorted(set(unmatched))
        print(f"WARNING: {len(unmatched)} Flipkart rows ({len(uniq)} distinct nh_sku) had no D2C match "
              f"-- not in {d2c_path}. First few: {uniq[:10]}")
    print(f"-> {out_path}")

    # quick flag summary, same spirit as the extraction_source diagnostic from the Flipkart sweep
    import collections
    n_broken = sum(1 for r in out_rows if r["flipkart_page_broken"])
    if n_broken:
        print(f"FLIPKART_PAGE_BROKEN: {n_broken}/{len(out_rows)} rows show Flipkart's generic "
              f"placeholder page instead of real product data -- title/description/ingredients/"
              f"shelf-life/image comparisons are skipped for these (nothing to compare against), "
              f"but they need a manual look on Flipkart's side (dead FSN, soft-block, or a real bug).")
    comparable = [r for r in out_rows if not r["flipkart_page_broken"]]
    print(f"image_count_flag mix (comparable rows only, n={len(comparable)}):",
          dict(collections.Counter(r["image_count_flag"] for r in comparable)))
    print("shelf_life_status mix (comparable rows only):",
          dict(collections.Counter(r["shelf_life_status"] for r in comparable)))
    not_plausible = sum(1 for r in comparable if r["title_plausible"] is False)
    print(f"titles flagged not-plausible (eyeball these, not a hard fail): {not_plausible}/{len(comparable)}")


# ===========================================================================
# Selftest -- synthetic but realistic fixtures, no network
# ===========================================================================
def selftest():
    checks = []

    d = description_similarity(
        "Flipkart.com: Buy Nat Habit Ubtan Face Wash for Rs. 269.0 from Flipkart.com. "
        "1. Brightens skin 2. Removes tan 3. Made with real dals and besan",
        "Experience the magic of true ubtan in a face wash. Made with real dals, besan, "
        "turmeric and chandan, it brightens and de-tans with every use.",
    )
    checks.append(("description_similarity: boilerplate stripped, real overlap scores decently on jaccard",
                    d["token_jaccard"] > 0.15))

    d0 = description_similarity("Flipkart.com: Buy X for Rs. 1 from Flipkart.com. Totally unrelated gadget copy.",
                                 "Completely different skincare marketing text about turmeric.")
    checks.append(("description_similarity: unrelated text scores low",
                    d0["token_jaccard"] < 0.2))

    ing = ingredients_coverage(
        "Contains wild kasturi, rakht chandan and besan for glow, plus rose petals extract.",
        "wild kasturi, rakht chandan, besan, moong, rose petals, multani",
    )
    checks.append(("ingredients_coverage: partial match counted correctly (4 of 6 terms present)",
                    ing == round(4 / 6, 3)))

    checks.append(("ingredients_coverage: None when D2C has no ingredient list (not a 0)",
                    ingredients_coverage("anything", "") is None))

    checks.append(("shelf_life_match: matching duration found",
                    shelf_life_match("Best used within 9 months of manufacturing.",
                                      "Use within 9 months of Mfg Date") == "match"))
    checks.append(("shelf_life_match: different duration flagged as mismatch",
                    shelf_life_match("Shelf life: 24 months from Mfg date.",
                                      "Use within 9 months of Mfg Date") == "mismatch"))
    checks.append(("shelf_life_match: no duration mentioned at all",
                    shelf_life_match("No expiry info in this description.",
                                      "Use within 9 months of Mfg Date") == "not_found"))
    checks.append(("shelf_life_match: '30 Day Replacement Guarantee' boilerplate "
                    "ignored, not treated as the product's actual duration",
                    shelf_life_match("Buy Nat Habit Lotion for Rs.503 online. "
                                      "30 Day Replacement Guarantee. Only Genuine Products.",
                                      "Use within 9 months of Mfd.") == "not_found"))
    checks.append(("shelf_life_match: boilerplate stripped but a real matching "
                    "duration elsewhere in the text still counts",
                    shelf_life_match("30 Day Replacement Guarantee. Best used within "
                                      "9 months of manufacturing for best results.",
                                      "Use within 9 months of Mfg Date") == "match"))
    checks.append(("shelf_life_match: 'children above 5 years' age warning "
                    "ignored, not mistaken for a 5-year shelf life",
                    shelf_life_match("Not suitable for children above 5 years. "
                                      "Made with natural henna.",
                                      "Use within 45 days of Mfg Date") == "not_found"))
    checks.append(("shelf_life_match: genuine conflicting duration still caught "
                    "(this is the real, useful signal)",
                    shelf_life_match("Use within 3 months of Mfg Date & keep away "
                                      "from sunlight.",
                                      "Use within 6 months of Mfg. Date") == "mismatch"))

    # --- structured Flipkart "Maximum Shelf Life" spec field (the fix for
    # the real gap you found: this scrapes from Flipkart's on-page
    # Specifications grid via flipkart_pdp_scraper.extract_max_shelf_life,
    # not the free-text description) -- takes priority over the description
    # scan above whenever it's present.
    checks.append(("shelf_life_match: structured Flipkart field matches D2C "
                    "(the real case: FSN FCWGUA62VWP9E37J, 'Maximum Shelf Life: "
                    "9 Months' on the public PDP, description contains no "
                    "duration at all)",
                    shelf_life_match("Ubtan face wash for glowing skin. No duration "
                                      "mentioned anywhere in this ad copy.",
                                      "Use within 9 months of Mfg Date",
                                      "9 Months") == "match"))
    checks.append(("shelf_life_match: structured field disagrees with D2C "
                    "-> mismatch even though description alone would say not_found",
                    shelf_life_match("No duration mentioned anywhere in this ad copy.",
                                      "Use within 12 months of Mfg Date",
                                      "9 Months") == "mismatch"))
    checks.append(("shelf_life_match: structured field present but unparseable "
                    "(e.g. 'Not Applicable' / 'NA') is reported honestly, not "
                    "silently treated as a description-scan fallback",
                    shelf_life_match("Best used within 9 months of manufacturing.",
                                      "Use within 9 months of Mfg Date",
                                      "Not Applicable") == "unparseable"))
    checks.append(("shelf_life_match: structured field wins over a *wrong* "
                    "description-scan result -- description mentions an unrelated "
                    "9-month warranty that would have falsely matched under the "
                    "old free-text-only logic, but the real spec field (12 months) "
                    "correctly reports mismatch",
                    shelf_life_match("9 month replacement warranty included. Great gift.",
                                      "Use within 9 months of Mfg Date",
                                      "12 Months") == "mismatch"))
    checks.append(("shelf_life_match: no structured field (older scrape, "
                    "column blank) falls back to the old description-scan "
                    "behavior unchanged",
                    shelf_life_match("Best used within 9 months of manufacturing.",
                                      "Use within 9 months of Mfg Date", "") == "match"))
    checks.append(("shelf_life_match: unit-aware structured comparison -- "
                    "'2 months' (D2C) and '60 Days' (Flipkart) are the same "
                    "duration, not a mismatch. Real production case: this and "
                    "6 other SKUs were wrongly flagged 'mismatch' before this "
                    "fix (raw (number, unit) tuples compared literally).",
                    shelf_life_match("irrelevant description text",
                                      "Use within 2 months of Mfg Date",
                                      "60 Days") == "match"))
    checks.append(("shelf_life_match: unit-aware comparison also catches a "
                    "genuine mismatch across units ('9 months' D2C vs '90 Days' "
                    "Flipkart -- 90 days is 3 months, not 9)",
                    shelf_life_match("irrelevant description text",
                                      "Use within 9 months of Mfg. Date.",
                                      "90 Days") == "mismatch"))

    t = title_sanity("Nat Habit Dual Tooth Neem Wooden Comb for Women & Men | Anti-Frizz & Dandruff Control",
                      "Neem Wooden Comb")
    checks.append(("title_sanity: SKU's core words present -> plausible even though titles differ",
                    t["plausible"] is True))
    t2 = title_sanity("Nat Habit Bamboo Charcoal Soap Bar 100g", "Ubtan Tikta Face Wash")
    checks.append(("title_sanity: unrelated product flagged as not plausible",
                    t2["plausible"] is False))

    checks.append(("image_count_flag: equal counts", image_count_flag(5, 5) == "match"))
    checks.append(("image_count_flag: off-by-one is minor", image_count_flag(4, 5) == "minor_mismatch"))
    checks.append(("image_count_flag: big gap is large_mismatch", image_count_flag(1, 5) == "large_mismatch"))
    checks.append(("image_count_flag: flipkart has zero images", image_count_flag(0, 5) == "flipkart_no_images"))
    checks.append(("image_count_flag: d2c reference has zero images", image_count_flag(5, 0) == "d2c_no_images"))

    checks.append(("is_flipkart_page_broken: catches the real generic placeholder title",
                    is_flipkart_page_broken("X Store Online - Buy X Online at Best Price in India | Flipkart.com") is True))
    checks.append(("is_flipkart_page_broken: a normal real title is not flagged",
                    is_flipkart_page_broken("Nat Habit Dual Tooth Neem Wooden Comb for Women & Men") is False))

    # Visual-hash checks -- only run if Pillow is installed (it's optional
    # unless you're using --visual). Uses synthetic in-memory images, no
    # network or real product photos needed. dHash is edge/gradient-based
    # (it compares each pixel to its neighbor), so a flat solid color always
    # hashes to the same all-zero value regardless of which color it is --
    # these fixtures use actual gradients/patterns so the comparisons are
    # meaningful.
    try:
        from PIL import Image

        def _save_and_hash(im):
            path = "/tmp/_qa_diff_selftest_img.png"
            im.save(path, format="PNG")
            return _phash(path)

        def _gradient(size, reverse=False):
            im = Image.new("L", (size, size))
            for x in range(size):
                v = int(255 * x / (size - 1))
                for y in range(size):
                    im.putpixel((x, y), 255 - v if reverse else v)
            return im.convert("RGB")

        def _checkerboard(size, block=8):
            im = Image.new("RGB", (size, size))
            for y in range(0, size, block):
                for x in range(0, size, block):
                    color = (0, 0, 0) if (x // block + y // block) % 2 else (255, 255, 255)
                    im.paste(color, (x, y, x + block, y + block))
            return im

        h_grad = _save_and_hash(_gradient(64))
        h_grad_resized = _save_and_hash(_gradient(256).resize((64, 64), Image.LANCZOS))
        h_grad_reversed = _save_and_hash(_gradient(64, reverse=True))
        h_checker = _save_and_hash(_checkerboard(64))

        checks.append(("_phash: identical image hashes to itself with zero distance",
                        _hamming(h_grad, h_grad) == 0))
        checks.append(("_phash: the same gradient survives a resize (near-identical hash) -- "
                        "the exact robustness this needs, since the same product photo gets "
                        "re-encoded at different resolutions by each platform",
                        _hamming(h_grad, h_grad_resized) <= 8))
        checks.append(("_phash: the reversed gradient (a genuinely different image) "
                        "produces a large Hamming distance",
                        _hamming(h_grad, h_grad_reversed) >= 40))
        checks.append(("_phash: an unrelated checkerboard pattern also reads as different",
                        _hamming(h_grad, h_checker) >= 20))

        # The realistic failure mode this needs to survive: a marketplace
        # pads a product photo to a required aspect ratio with solid-color
        # bars; the brand's own D2C photo of the same shot has no such
        # padding. Same picture, different border -- must still read as a
        # near-match, not as two different photos.
        def _bordered_subject(w, h, bg):
            im = Image.new("L", (w, h), bg)
            bw, bh = int(w * 0.6), int(h * 0.6)
            ox, oy = (w - bw) // 2, (h - bh) // 2
            for x in range(bw):
                v = int(255 * x / (bw - 1))
                for y in range(bh):
                    im.putpixel((ox + x, oy + y), v)
            return im

        d2c_photo = _bordered_subject(100, 150, bg=200)   # unpadded, own bg
        fk_photo = Image.new("L", (150, 150), 255)        # letterboxed to square
        fk_photo.paste(d2c_photo, (25, 0))

        h_d2c_photo = _save_and_hash(d2c_photo.convert("RGB"))
        h_fk_photo = _save_and_hash(fk_photo.convert("RGB"))
        checks.append(("_phash: same underlying product photo survives one platform "
                        "letterboxing it to a square while the other doesn't crop/pad it "
                        "-- this is the real, common cross-platform case, not a synthetic edge case",
                        _hamming(h_d2c_photo, h_fk_photo) <= 8))
    except ImportError:
        print("  [SKIP] visual-hash checks -- Pillow not installed (only needed for --visual)")

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
    ap.add_argument("--flipkart", default=FLIPKART_DEFAULT)
    ap.add_argument("--d2c", default=D2C_DEFAULT)
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--visual", action="store_true",
                     help="also download images and compare them perceptually "
                          "(needs network + `pip install pillow`)")
    a = ap.parse_args()

    if a.selftest:
        selftest()
        sys.exit(0)

    run(a.flipkart, a.d2c, a.out, do_visual=a.visual)
