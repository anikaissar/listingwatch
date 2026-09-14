#!/usr/bin/env python3
"""
Myntra PDP scraper for the Listing-QA sweep (observed content side) --
Myntra's counterpart to flipkart_pdp_scraper.py and nykaa_pdp_scraper.py.

Reads myntra_worklist.csv and, for each Myntra style ID, loads the public PDP
and extracts OBSERVED content: title, image count, description, ingredients,
shelf life, and stock status. Myntra product pages are public (no login
required to view), same as Nykaa.

URL FORMAT (confirmed by Anika, 2026-09-14): a bare style ID is enough --
https://www.myntra.com/<style_id> -- no slug/category/brand path needed.

HEADLESS BLOCKED (confirmed 2026-09-14, real test): headless Chromium hit
net::ERR_HTTP2_PROTOCOL_ERROR on the very first real style ID tried -- the
exact same failure signature Nykaa gave. Headed mode worked immediately on
every URL tried afterward. Same operational constraint as Nykaa: this only
works in an interactive desktop session (Task Scheduler must be "Run only
when user is logged on"), not "Run whether user is logged on or not".

EXTRACTION SOURCE: Myntra embeds a rich state blob in
`window.__myx = {"pdpData": {...}, ...}`. Unlike Flipkart/Nykaa, this is the
ONLY source used here -- no JSON-LD/meta fallback chain -- because it was
confirmed present and complete on every real page checked (2 live products,
2 real-but-out-of-stock products, 1 confirmed-live-but-D2C-discontinued
product, and 1 genuinely nonexistent style ID), and it's far richer than
Myntra's own JSON-LD (whose "description" field is just the title again,
not real content).

PAGE-NOT-FOUND FINDING (confirmed 2026-09-14, style ID 999999999 -- a
deliberately bogus ID, since none of the real SKUs tried turned out to be
actually dead on Myntra): pdpData is completely absent (None) and the page
<title> collapses to the generic "Product Details" instead of a real SEO
title. is_page_not_found() reads pdpData's absence directly.

SHELF-LIFE FINDING (confirmed 2026-09-14, both real sample products):
UNLIKE Nykaa, Myntra's articleAttributes carries real, populated shelf-life
fields: "Minimum Shelf Life in Months" and "Total Shelf Life in Months".
Per Anika's explicit instruction, only "Total Shelf Life in Months" (the
maximum-duration one, matching the semantics of Flipkart's "Maximum Shelf
Life" spec field) is used for the D2C comparison -- "Minimum Shelf Life in
Months" is not compared against anything, just carried for reference. This
means Myntra DOES get a real P1 Shelf Life Mismatch tier, unlike Nykaa.

INGREDIENTS: articleAttributes["Ingredients"] is a real, populated
comma-separated list on both real products checked -- captured separately
(obs_ingredients) AND folded into obs_description_text (see
_build_description_text()) so qa_diff.py's shared ingredients_coverage()
(which only takes one description-text argument) has the best chance of
matching D2C's own ingredient terms.

DESCRIPTION: Myntra has no single rich free-text description field --
articleAttributes carries the real descriptive content spread across
category-dependent keys (Benefits, How-to-Apply, About the Brand, Concerns,
Application Time, etc. -- the exact key set varies by product category).
_build_description_text() joins every articleAttributes value (stripped of
HTML tags), EXCLUDING only the numeric shelf-life/usable-period keys, so it
works regardless of which descriptive keys a given product happens to have.

STOCK STATUS: pdpData.flags.outOfStock (bool) -- a real page that's simply
out of stock right now is NOT "broken" (same design as Flipkart/Nykaa) --
its title/description/images/ingredients/shelf-life are all still real and
worth comparing.

NETWORK: the live scrape hits myntra.com, unreachable from this sandbox.
Run in your environment. Use --selftest to validate the parser against
bundled fixtures built from the real field structure confirmed above.
"""

import argparse
import asyncio
import csv
import json
import os
import random
import re
import sys

OUT_DEFAULT = "observed_myntra_latest.csv"
FAIL_DEFAULT = "observed_myntra_failures.csv"

OUT_COLS = [
    "nh_sku", "style_id", "pdp_availability", "obs_title", "obs_image_count",
    "obs_image_urls",
    "obs_description_len", "obs_description_text", "obs_ingredients", "obs_price",
    "extraction_source", "obs_shelf_life", "obs_max_shelf_life",
    "obs_page_not_found", "scraped_at",
]

# ---- tunables ----
CONCURRENCY = 3
MIN_DELAY, MAX_DELAY = 1.5, 3.5     # seconds between page loads, per worker
NAV_TIMEOUT_MS = 30000
MAX_RETRIES = 2


# ===========================================================================
# Parsing -- pure functions, unit-testable without a browser
# ===========================================================================
def _clean(text):
    return " ".join((text or "").split())


def _strip_tags(html_fragment):
    return _clean(re.sub(r"<[^>]+>", " ", html_fragment or ""))


_MYX_RE = re.compile(r"window\.__myx\s*=\s*")


def _parse_myx_raw(html):
    """Locate and JSON-parse Myntra's own embedded `window.__myx = {...}`
    blob (brace-balanced, same technique as nykaa_pdp_scraper.py's
    _parse_preloaded_state_raw -- respects quoted strings so braces inside
    description/attribute text don't throw off the scan). Returns the
    parsed dict, or None if the blob isn't present or doesn't parse."""
    m = _MYX_RE.search(html)
    if not m or m.end() >= len(html) or html[m.end()] != "{":
        return None

    start = m.end()
    i = start
    n = len(html)
    depth = 0
    in_str = False
    while i < n:
        c = html[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    blob = html[start:i]

    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


def is_page_not_found(html, state=None):
    """True when Myntra's own pdpData is completely absent -- confirmed
    (2026-09-14) against a deliberately bogus style ID (999999999): pdpData
    is None and the page <title> collapses to the generic "Product Details"
    instead of a real SEO title. Every real product page checked (including
    genuinely out-of-stock ones) always has a populated pdpData.

    Pass `state` (an already-parsed blob from _parse_myx_raw) to avoid
    re-parsing when the caller also needs parse_myx_pdp_data().

    Returns True/False, or None if the __myx blob wasn't found at all (in
    which case the caller should fall back to other signals, e.g. no title
    anywhere -> "no_content")."""
    data = state if state is not None else _parse_myx_raw(html)
    if data is None:
        return None
    return data.get("pdpData") is None


# Numeric/duration attribute keys -- kept out of the free-text description
# blob (see _build_description_text()) since they're structured facts, not
# descriptive prose. Ingredients/Key Ingredients are deliberately NOT in this
# exclusion set -- they're folded INTO the description text so qa_diff.py's
# shared ingredients_coverage() (single description-text argument) has the
# best chance of matching D2C's own ingredient terms against it.
_NON_DESCRIPTIVE_ATTR_KEYS = {
    "Minimum Shelf Life in Months", "Total Shelf Life in Months",
    "Minimum Usable Period in Months",
}


def _build_description_text(article_attributes):
    """Myntra has no single rich free-text description field (its own
    JSON-LD "description" is just the title again) -- the real descriptive
    content lives spread across articleAttributes under a category-dependent
    key set (Benefits, How-to-Apply, About the Brand, Concerns, Application
    Time, etc. -- confirmed to vary between a hair-oil and a face-mask
    product, 2026-09-14). Joining every value (HTML-stripped), except the
    numeric shelf-life keys, works regardless of which descriptive keys a
    given product category happens to populate. Keys are sorted for a
    deterministic join order across scrapes (Myntra's own key order isn't
    guaranteed stable)."""
    aa = article_attributes or {}
    parts = []
    for k in sorted(aa.keys()):
        if k in _NON_DESCRIPTIVE_ATTR_KEYS:
            continue
        v = aa[k]
        if isinstance(v, str) and v.strip():
            parts.append(_strip_tags(v))
    return " ".join(parts)


_SHELF_LIFE_MONTHS_RE = re.compile(r"\d+")


def _shelf_life_months_to_text(value):
    """articleAttributes["Total Shelf Life in Months"] is a bare number
    (e.g. "6" or 6) -- reformat as "<n> Months" so it matches qa_diff.py's
    shared _DURATION_RE ("\\d+\\s*(day|days|month|months|year|years)") the
    same way Flipkart's obs_max_shelf_life text does."""
    if value is None:
        return ""
    m = _SHELF_LIFE_MONTHS_RE.search(str(value))
    return f"{m.group(0)} Months" if m else ""


def _dedupe_images(albums):
    """Sum images across every album (confirmed 2026-09-14: real pages can
    have more than one album, e.g. a populated "default" gallery plus an
    empty "animatedImage" album) rather than assuming only the first album
    matters, deduping by imageURL (the one clean, directly-fetchable field
    on each image entry -- src/secureSrc carry unresolved
    $width/$height/$qualityPercentage template placeholders)."""
    seen = []
    for album in albums or []:
        for img in album.get("images") or []:
            url = img.get("imageURL")
            if url and url not in seen:
                seen.append(url)
    return seen


def extract(html):
    """Merge Myntra's __myx state into a single observed record.

    Check order: page-not-found (pdpData missing) wins over everything
    else, since a delisted style ID has no meaningful title/image/
    availability data to report -- see is_page_not_found()'s docstring."""
    raw_state = _parse_myx_raw(html)
    not_found = is_page_not_found(html, state=raw_state)

    if not_found or raw_state is None:
        # raw_state is None means the __myx blob itself wasn't found at all
        # (e.g. a genuinely different page structure) -- no usable data
        # either way, same practical effect as a confirmed page-not-found.
        return {
            "pdp_availability": "page_not_found" if not_found else "no_content",
            "obs_title": "", "obs_image_count": 0, "obs_image_urls": "",
            "obs_description_len": 0, "obs_description_text": "",
            "obs_ingredients": "", "obs_price": "",
            "extraction_source": "none",
            "obs_shelf_life": "", "obs_max_shelf_life": "",
            "obs_page_not_found": bool(not_found),
        }

    pdp = raw_state.get("pdpData") or {}
    title = _clean(pdp.get("name", ""))
    if not title:
        return {
            "pdp_availability": "no_content",
            "obs_title": "", "obs_image_count": 0, "obs_image_urls": "",
            "obs_description_len": 0, "obs_description_text": "",
            "obs_ingredients": "", "obs_price": "",
            "extraction_source": "none",
            "obs_shelf_life": "", "obs_max_shelf_life": "",
            "obs_page_not_found": False,
        }

    aa = pdp.get("articleAttributes") or {}
    images = _dedupe_images((pdp.get("media") or {}).get("albums"))
    # Confirmed against real data (2026-09-14, style IDs 36938665/20188914):
    # this field isn't always plain comma-separated text -- it can carry its
    # own HTML markup (<b>/<ul>/<li>), so strip tags here too, same as every
    # other articleAttributes value.
    ingredients = _strip_tags(aa.get("Ingredients", ""))
    desc = _build_description_text(aa)
    max_shelf_life = _shelf_life_months_to_text(aa.get("Total Shelf Life in Months"))
    min_shelf_life = _shelf_life_months_to_text(aa.get("Minimum Shelf Life in Months"))
    price = (pdp.get("price") or {}).get("discounted", "")
    out_of_stock = bool((pdp.get("flags") or {}).get("outOfStock"))

    if out_of_stock:
        return {
            "pdp_availability": "unavailable",
            "obs_title": title,
            "obs_image_count": len(images), "obs_image_urls": "|".join(images),
            "obs_description_len": len(desc), "obs_description_text": desc,
            "obs_ingredients": ingredients, "obs_price": str(price),
            "extraction_source": "myx",
            # Real page, just out of stock -- shelf life/ingredients/images
            # are all still real data, worth keeping (same design as
            # Flipkart/Nykaa: only a BROKEN page blanks these out).
            "obs_shelf_life": min_shelf_life, "obs_max_shelf_life": max_shelf_life,
            "obs_page_not_found": False,
        }

    return {
        "pdp_availability": "available",
        "obs_title": title,
        "obs_image_count": len(images), "obs_image_urls": "|".join(images),
        "obs_description_len": len(desc), "obs_description_text": desc,
        "obs_ingredients": ingredients, "obs_price": str(price),
        "extraction_source": "myx",
        "obs_shelf_life": min_shelf_life, "obs_max_shelf_life": max_shelf_life,
        "obs_page_not_found": False,
    }


# ===========================================================================
# Scrape orchestration (async Playwright)
# ===========================================================================
def load_worklist(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_done(out_path):
    if not os.path.exists(out_path):
        return set()
    with open(out_path, newline="", encoding="utf-8") as f:
        return {r["style_id"] for r in csv.DictReader(f) if r.get("style_id")}


async def _scrape_one(context, row, out_writer, fail_writer, lock):
    import datetime
    style_id, nh, url = row["style_id"], row["nh_sku"], row["pdp_url"]
    for attempt in range(1, MAX_RETRIES + 1):
        page = await context.new_page()
        try:
            await page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            html = await page.content()
            rec = extract(html)
            rec.update({
                "nh_sku": nh, "style_id": style_id,
                "scraped_at": datetime.datetime.now().isoformat(timespec="seconds"),
            })
            async with lock:
                out_writer.writerow(rec)
            await page.close()
            return True
        except Exception as e:  # noqa: BLE001
            await page.close()
            if attempt == MAX_RETRIES:
                async with lock:
                    fail_writer.writerow({"nh_sku": nh, "style_id": style_id, "url": url,
                                          "error": repr(e)[:200]})
                return False
            await asyncio.sleep(2 * attempt)


async def _worker(name, queue, context, out_writer, fail_writer, lock, counter):
    while True:
        try:
            row = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        ok = await _scrape_one(context, row, out_writer, fail_writer, lock)
        if not ok:
            counter["failed"] += 1
        counter["done"] += 1
        if counter["done"] % 25 == 0:
            print(f"  ...{counter['done']}/{counter['total']} scraped", file=sys.stderr)
        await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


async def run(worklist_path, out_path, fail_path, limit=None, headed=True):
    from playwright.async_api import async_playwright
    work = load_worklist(worklist_path)
    done = load_done(out_path)
    todo = [r for r in work if r["style_id"] not in done]
    if limit:
        todo = todo[:limit]
    print(f"Worklist {len(work)} | already done {len(done)} | to scrape {len(todo)}")
    if not todo:
        print("Nothing to do.")
        return

    out_new = not os.path.exists(out_path)
    fail_new = not os.path.exists(fail_path)
    out_f = open(out_path, "a", newline="", encoding="utf-8")
    fail_f = open(fail_path, "a", newline="", encoding="utf-8")
    out_writer = csv.DictWriter(out_f, fieldnames=OUT_COLS)
    fail_writer = csv.DictWriter(fail_f, fieldnames=["nh_sku", "style_id", "url", "error"])
    if out_new:
        out_writer.writeheader()
    if fail_new:
        fail_writer.writeheader()

    queue = asyncio.Queue()
    for r in todo:
        queue.put_nowait(r)
    lock = asyncio.Lock()
    counter = {"done": 0, "total": len(todo), "failed": 0}

    async with async_playwright() as p:
        # headed=True by default -- confirmed 2026-09-14 that headless hits
        # net::ERR_HTTP2_PROTOCOL_ERROR on Myntra, the same signature Nykaa
        # gave. Same operational constraint: needs an interactive desktop
        # session, Task Scheduler must be "Run only when user is logged on".
        browser = await p.chromium.launch(headless=not headed)
        ctx_kwargs = {
            "locale": "en-IN",
            "viewport": {"width": 1366, "height": 900},
            "extra_http_headers": {"Accept-Language": "en-IN,en;q=0.9"},
        }
        context = await browser.new_context(**ctx_kwargs)
        workers = [
            asyncio.create_task(
                _worker(f"w{i}", queue, context, out_writer, fail_writer, lock, counter)
            )
            for i in range(CONCURRENCY)
        ]
        await asyncio.gather(*workers)
        await browser.close()

    out_f.close()
    fail_f.close()
    print(f"Done. Observed -> {out_path} | failures -> {fail_path} "
          f"({counter['failed']}/{counter['total']} rows failed this run)")


# ===========================================================================
# Self-test (no browser, no network) -- fixtures built from the real field
# structure confirmed against 6 actual Myntra HTML dumps, 2026-09-14: two
# live in-stock products, two real-but-out-of-stock products, one live
# product whose SKU is already confirmed discontinued on D2C, and one
# deliberately bogus style ID.
# ===========================================================================
FIXTURE_LIVE = """
<html><head><title>Buy Nat Habit Hibiscus Amla Summer Dasabuti Hair Oil 200ml | Myntra</title></head>
<body><script>window.__myx = {"pdpData":{"id":20186078,
"name":"Nat Habit Hibiscus Amla Summer Dasabuti Hair Oil for Hair Growth & Fall Control - 200ml",
"articleAttributes":{
  "About the Brand":"<p>Freshly made beauty products.</p>",
  "Benefits":"<ul><li>Severe Hairfall Control</li></ul>",
  "Ingredients":"Hibiscus flower, hibiscus leaves, bel leaves, tulsi, brahmi, amla, neem",
  "Key Ingredients":"Amla",
  "Minimum Shelf Life in Months":"5",
  "Total Shelf Life in Months":"6"
},
"media":{"albums":[{"name":"default","images":[
  {"imageURL":"http://assets.myntassets.com/a.jpg"},
  {"imageURL":"http://assets.myntassets.com/b.jpg"},
  {"imageURL":"http://assets.myntassets.com/c.jpg"}
]},{"name":"animatedImage","images":[]}]},
"flags":{"outOfStock":false},
"price":{"mrp":696,"discounted":550}
}};</script></body></html>
"""

FIXTURE_HTML_IN_INGREDIENTS = """
<html><head><title>Buy Nat Habit Grapeseed Body Oil 30ml | Myntra</title></head>
<body><script>window.__myx = {"pdpData":{"id":20191182,
"name":"Nat Habit 100% Pure Cold Pressed Grapeseed Body Oil for Dark Spot Treatment - 30 ml",
"articleAttributes":{
  "Ingredients":"<b>Grapeseed Oil:</b> This antioxidant may even out skin tone.",
  "Total Shelf Life in Months":"12",
  "Minimum Shelf Life in Months":"10"
},
"media":{"albums":[{"name":"default","images":[{"imageURL":"http://assets.myntassets.com/e.jpg"}]}]},
"flags":{"outOfStock":true},
"price":{"mrp":459,"discounted":459}
}};</script></body></html>
"""

FIXTURE_OUT_OF_STOCK = """
<html><head><title>Buy Nat Habit Moong Nourish Bath Ubtan 80g | Myntra</title></head>
<body><script>window.__myx = {"pdpData":{"id":20188914,
"name":"Nat Habit Moong Nourish Bath Ubtan Skin Polishing & Brightening-80g",
"articleAttributes":{
  "Benefits":"Polishes and brightens skin.",
  "Ingredients":"Moong dal, sandalwood, rose petals",
  "Total Shelf Life in Months":"24",
  "Minimum Shelf Life in Months":"12"
},
"media":{"albums":[{"name":"default","images":[{"imageURL":"http://assets.myntassets.com/d.jpg"}]}]},
"flags":{"outOfStock":true},
"price":{"mrp":300,"discounted":250}
}};</script></body></html>
"""

# Reproduces the real confirmed dead-style-ID case (2026-09-14, style ID
# 999999999, deliberately bogus): pdpData is completely absent and the page
# <title> collapses to the generic "Product Details" -- no real product data
# anywhere on the page.
FIXTURE_PAGE_NOT_FOUND = """
<html><head><title>Product Details</title></head>
<body><script>window.__myx = {"pdpData":null,"pageName":"pdp","dataExpired":false};</script></body></html>
"""

# A page with no window.__myx blob at all (different failure mode from a
# confirmed page-not-found -- e.g. a totally different page template, or a
# network hiccup that served something else entirely). Treated as
# "no_content" rather than "page_not_found" since we can't confirm which one
# it actually is.
FIXTURE_NO_MYX_BLOB = """
<html><head><title>Myntra</title></head><body>Some unrelated page.</body></html>
"""


def selftest():
    checks = []

    live = extract(FIXTURE_LIVE)
    checks.append(("live: available", live["pdp_availability"] == "available"))
    checks.append(("live: title read from pdpData.name",
                    live["obs_title"] == "Nat Habit Hibiscus Amla Summer Dasabuti Hair Oil for Hair Growth & Fall Control - 200ml"))
    checks.append(("live: image count summed across albums, deduped", live["obs_image_count"] == 3))
    checks.append(("live: Total Shelf Life in Months reformatted to match qa_diff's duration regex",
                    live["obs_max_shelf_life"] == "6 Months"))
    checks.append(("live: Minimum Shelf Life carried in obs_shelf_life (reference only, not compared)",
                    live["obs_shelf_life"] == "5 Months"))
    checks.append(("live: Ingredients folded into description text for ingredients_coverage()",
                    "hibiscus flower" in live["obs_description_text"].lower()))
    checks.append(("live: Benefits folded into description text too",
                    "hairfall control" in live["obs_description_text"].lower()))
    checks.append(("live: obs_ingredients captured separately", live["obs_ingredients"].startswith("Hibiscus flower")))
    checks.append(("live: not page_not_found", live["obs_page_not_found"] is False))

    oos = extract(FIXTURE_OUT_OF_STOCK)
    checks.append(("out-of-stock: real page, NOT broken -- pdp_availability is 'unavailable'",
                    oos["pdp_availability"] == "unavailable"))
    checks.append(("out-of-stock: title/shelf-life/ingredients still real data (only a BROKEN "
                    "page blanks these out, same design as Flipkart/Nykaa)",
                    oos["obs_title"] and oos["obs_max_shelf_life"] == "24 Months" and oos["obs_ingredients"]))
    checks.append(("out-of-stock: not page_not_found", oos["obs_page_not_found"] is False))

    html_ing = extract(FIXTURE_HTML_IN_INGREDIENTS)
    checks.append(("obs_ingredients strips its own embedded HTML markup (confirmed real case, "
                    "style ID 20191182 -- the field isn't always plain comma-separated text)",
                    "<b>" not in html_ing["obs_ingredients"] and html_ing["obs_ingredients"].startswith("Grapeseed Oil:")))

    not_found = extract(FIXTURE_PAGE_NOT_FOUND)
    checks.append(("page_not_found: pdpData missing -> page_not_found",
                    not_found["pdp_availability"] == "page_not_found"))
    checks.append(("page_not_found: obs_page_not_found is True", not_found["obs_page_not_found"] is True))
    checks.append(("page_not_found: no title/images/shelf-life data",
                    not_found["obs_title"] == "" and not_found["obs_image_count"] == 0
                    and not_found["obs_max_shelf_life"] == ""))

    no_blob = extract(FIXTURE_NO_MYX_BLOB)
    checks.append(("no __myx blob at all: no_content (distinct from a confirmed page_not_found)",
                    no_blob["pdp_availability"] == "no_content"))
    checks.append(("no __myx blob: obs_page_not_found is False (we don't actually know it's dead, "
                    "just that this page has no usable data)", no_blob["obs_page_not_found"] is False))

    ok = True
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print("SELFTEST", "PASSED" if ok else "FAILED")
    if not ok:
        sys.exit(1)


# ===========================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="validate parser on fixtures")
    ap.add_argument("--worklist", default="myntra_worklist.csv")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--failures", default=FAIL_DEFAULT)
    ap.add_argument("--limit", type=int, help="scrape only the first N (smoke test)")
    ap.add_argument("--headless", action="store_true",
                     help="force headless despite the confirmed HTTP2 block -- for re-testing "
                          "only, expect every row to fail")
    a = ap.parse_args()

    if a.selftest:
        selftest()
    else:
        asyncio.run(run(a.worklist, a.out, a.failures, a.limit, headed=not a.headless))
