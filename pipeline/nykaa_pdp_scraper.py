#!/usr/bin/env python3
"""
Nykaa PDP scraper for the Listing-QA sweep (observed content side) --
Nykaa's counterpart to flipkart_pdp_scraper.py.

Reads nykaa_worklist.csv and, for each Nykaa product ID, loads the public PDP
and extracts OBSERVED content: title, image count, description, price, and a
best-effort shelf-life/expiry read. Nykaa product pages are public (no login
required to view), unlike Flipkart.

Extraction strategy (same resilience-first approach as the Flipkart scraper):
  1. JSON-LD  <script type="application/ld+json"> Product block  -> name, image[], description, price
  2. og:/meta tags                                              -> title, image, description
  3. Whole-page text regex scan for shelf-life/expiry phrasing   -> best-effort only

IMPORTANT CAVEAT (read before trusting obs_shelf_life in bulk):
Unlike the Flipkart scraper's obs_max_shelf_life field -- which was built and
validated against real production HTML (Flipkart's structured Specifications
grid, see flipkart_pdp_scraper.py's extract_specifications()) -- this file's
shelf-life extraction (_extract_shelf_life_text) has NOT yet been validated
against a real, live Nykaa PDP's raw HTML. It was written from general
knowledge of common Nykaa page phrasing ("Shelf Life", "Best Before", "Use
By", "Expiry") without being able to inspect an actual page's DOM/JSON
structure from the sandbox this was authored in. Treat obs_shelf_life as a
best-effort field until it's been checked against a handful of real scraped
rows -- if it comes back empty or wrong across the board, that's a signal
this regex needs to be rewritten against Nykaa's actual page structure,
exactly the same kind of gap flipkart_pdp_scraper.py's docstring describes
having hit and fixed for Flipkart's own Specifications grid.

Your own nykaa_worklist.csv already carries a `master_shelf_life_days`
column (from your internal product master, e.g. 360 days), which is a much
more reliable ground truth than anything scraped off the live page -- prefer
diffing against that column over obs_shelf_life once we wire this into
qa_diff.py, rather than relying on this scraper to have correctly read
Nykaa's page.

NETWORK: the live scrape hits nykaa.com, unreachable from this sandbox. Run
in your environment. Use --selftest to validate the parser against a bundled
fixture (JSON-LD/meta only -- there is no real-HTML shelf-life fixture yet,
see caveat above).
"""

import argparse
import asyncio
import csv
import json
import os
import random
import re
import sys

OUT_DEFAULT = "observed_nykaa_latest.csv"
FAIL_DEFAULT = "observed_nykaa_failures.csv"

OUT_COLS = [
    "nh_sku", "nykaa_product_id", "pdp_availability", "obs_title", "obs_image_count",
    "obs_image_urls",
    "obs_description_len", "obs_description_text", "obs_price",
    "extraction_source", "obs_shelf_life", "scraped_at",
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


def parse_jsonld(html):
    """Return dict with title/images/description/price from a Product JSON-LD, or {}.
    Same approach as flipkart_pdp_scraper.py's parse_jsonld() -- this is a
    generic schema.org Product-microdata reader, not Flipkart-specific, so it
    should work unchanged against any site (Nykaa included) that publishes
    standard Product JSON-LD for SEO/rich-snippet purposes."""
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    )
    for raw in blocks:
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for c in list(candidates):
            if isinstance(c, dict) and "@graph" in c:
                candidates.extend(c["@graph"])
        for c in candidates:
            if not isinstance(c, dict):
                continue
            if c.get("@type") in ("Product", ["Product"]) or "name" in c and "offers" in c:
                imgs = c.get("image", [])
                if isinstance(imgs, str):
                    imgs = [imgs]
                offers = c.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = offers.get("price", "") if isinstance(offers, dict) else ""
                return {
                    "title": _clean(c.get("name", "")),
                    "images": [i for i in imgs if i],
                    "description": _clean(c.get("description", "")),
                    "price": str(price),
                    "source": "json-ld",
                }
    return {}


def parse_meta(html):
    """og:/meta fallback. Same generic approach as the Flipkart scraper."""
    def meta(prop):
        m = re.search(
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\'](.*?)["\']',
            html, re.IGNORECASE,
        )
        return _clean(m.group(1)) if m else ""
    title = meta("og:title") or meta("twitter:title")
    desc = meta("og:description") or meta("description")
    img = meta("og:image")
    if not title:
        return {}
    return {
        "title": title,
        "images": [img] if img else [],
        "description": desc,
        "price": meta("product:price:amount"),
        "source": "meta",
    }


# Best-effort shelf-life text patterns. UNVALIDATED against real Nykaa HTML --
# see the module docstring's caveat. Looks for a nearby duration (N days /
# months / years) following common shelf-life/expiry phrasing anywhere in the
# page text. First match wins.
_SHELF_LIFE_PATTERNS = [
    r"shelf\s*life[^0-9A-Za-z]{0,20}(\d+\s*(?:days?|months?|years?))",
    r"best\s*before[^0-9A-Za-z]{0,20}(\d+\s*(?:days?|months?|years?))",
    r"use\s*by[^0-9A-Za-z]{0,20}(\d+\s*(?:days?|months?|years?))",
    r"expir(?:y|es|ation)[^0-9A-Za-z]{0,20}(\d+\s*(?:days?|months?|years?))",
]


def extract_shelf_life_text(html):
    """Best-effort only -- see module docstring caveat. Strips tags to plain
    text first so phrasing split across nested elements still matches."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    for pat in _SHELF_LIFE_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return _clean(m.group(1))
    return ""


def detect_unavailable(html):
    """Heuristic: is this a dead/suppressed PDP rather than a live product?"""
    low = html.lower()
    signals = [
        "product is currently unavailable",
        "out of stock",
        "page not found",
        "sorry, this product",
        "we couldn't find that page",
    ]
    return any(s in low for s in signals)


def extract(html):
    """Merge strategies into a single observed record."""
    if detect_unavailable(html):
        base = parse_jsonld(html) or parse_meta(html)
        images = base.get("images", [])
        return {
            "pdp_availability": "unavailable",
            "obs_title": base.get("title", ""),
            "obs_image_count": len(images),
            "obs_image_urls": "|".join(images),
            "obs_description_len": len(base.get("description", "")),
            "obs_description_text": base.get("description", ""),
            "obs_price": base.get("price", ""),
            "extraction_source": base.get("source", "none"),
            "obs_shelf_life": "",
        }
    j = parse_jsonld(html)
    m = parse_meta(html)
    primary = j if j.get("title") else m
    if not primary.get("title"):
        return {
            "pdp_availability": "no_content",
            "obs_title": "", "obs_image_count": 0, "obs_image_urls": "",
            "obs_description_len": 0,
            "obs_description_text": "", "obs_price": "", "extraction_source": "none",
            "obs_shelf_life": "",
        }
    images = j.get("images") or m.get("images") or []
    desc = j.get("description") or m.get("description") or ""
    price = j.get("price") or m.get("price") or ""
    return {
        "pdp_availability": "available",
        "obs_title": primary["title"],
        "obs_image_count": len(images),
        "obs_image_urls": "|".join(images),
        "obs_description_len": len(desc),
        "obs_description_text": desc,
        "obs_price": price,
        "extraction_source": primary["source"],
        "obs_shelf_life": extract_shelf_life_text(html),
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
        return {r["nykaa_product_id"] for r in csv.DictReader(f) if r.get("nykaa_product_id")}


async def _scrape_one(context, row, out_writer, fail_writer, lock):
    import datetime
    pid, nh, url = row["nykaa_product_id"], row["nh_sku"], row["pdp_url"]
    for attempt in range(1, MAX_RETRIES + 1):
        page = await context.new_page()
        try:
            await page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            html = await page.content()
            rec = extract(html)
            rec.update({
                "nh_sku": nh, "nykaa_product_id": pid,
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
                    fail_writer.writerow({"nh_sku": nh, "nykaa_product_id": pid, "url": url,
                                          "error": repr(e)[:200]})
                return False
            await asyncio.sleep(2 * attempt)


async def _worker(name, queue, context, out_writer, fail_writer, lock, counter):
    while True:
        try:
            row = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        await _scrape_one(context, row, out_writer, fail_writer, lock)
        counter["done"] += 1
        if counter["done"] % 25 == 0:
            print(f"  ...{counter['done']}/{counter['total']} scraped", file=sys.stderr)
        await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


async def run(worklist_path, out_path, fail_path, limit=None):
    from playwright.async_api import async_playwright
    work = load_worklist(worklist_path)
    done = load_done(out_path)
    todo = [r for r in work if r["nykaa_product_id"] not in done]
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
    fail_writer = csv.DictWriter(fail_f, fieldnames=["nh_sku", "nykaa_product_id", "url", "error"])
    if out_new:
        out_writer.writeheader()
    if fail_new:
        fail_writer.writeheader()

    queue = asyncio.Queue()
    for r in todo:
        queue.put_nowait(r)
    lock = asyncio.Lock()
    counter = {"done": 0, "total": len(todo)}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx_kwargs = {"user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                                     "Chrome/124.0 Safari/537.36")}
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
    print(f"Done. Observed -> {out_path} | failures -> {fail_path}")


# ===========================================================================
# Self-test (no browser, no network) -- JSON-LD/meta parsing only.
# There is deliberately no real-HTML shelf-life fixture here yet (unlike
# Flipkart's FIXTURE_WITH_SPECS, which came from a real page source) --
# see the module docstring's caveat about obs_shelf_life needing validation
# against actual scraped rows before it can be trusted.
# ===========================================================================
FIXTURE_LIVE = """
<html><head>
<meta property="og:title" content="Nat Habit Five Oil Hibiscus Shampoo 250ml">
<script type="application/ld+json">
{"@type":"Product","name":"Nat Habit Five Oil Hibiscus Shampoo For Long Thick Hair 250ml",
 "image":["https://nykaa/a.jpg","https://nykaa/b.jpg","https://nykaa/c.jpg"],
 "description":"Sulphate free shampoo for hair fall control with five natural oils.",
 "offers":{"@type":"Offer","price":"366","priceCurrency":"INR"}}
</script></head><body>...</body></html>
"""

FIXTURE_META_ONLY = """
<html><head>
<meta property="og:title" content="Nat Habit Ubtan Face Wash 100g">
<meta property="og:image" content="https://nykaa/ubtan.jpg">
<meta name="description" content="Ubtan face wash with turmeric for glowing skin.">
</head><body></body></html>
"""

FIXTURE_DEAD = """
<html><head><title>Nykaa</title></head>
<body><div>Sorry, this product is currently unavailable.</div></body></html>
"""

FIXTURE_WITH_SHELF_LIFE_TEXT = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Nat Habit Rice Face Serum 30ml",
 "image":["https://nykaa/serum.jpg"],
 "description":"Brightening rice serum.",
 "offers":{"@type":"Offer","price":"499"}}
</script></head><body>
<div class="product-details">Shelf Life: 12 months from date of manufacture.</div>
</body></html>
"""


def selftest():
    live = extract(FIXTURE_LIVE)
    assert live["pdp_availability"] == "available", live
    assert live["obs_title"] == "Nat Habit Five Oil Hibiscus Shampoo For Long Thick Hair 250ml", live
    assert live["obs_image_count"] == 3, live
    assert live["extraction_source"] == "json-ld", live
    print("PASS  live JSON-LD:", live["obs_title"], "| imgs", live["obs_image_count"])

    meta = extract(FIXTURE_META_ONLY)
    assert meta["pdp_availability"] == "available", meta
    assert meta["obs_title"] == "Nat Habit Ubtan Face Wash 100g", meta
    assert meta["extraction_source"] == "meta", meta
    print("PASS  meta fallback:", meta["obs_title"], "| src", meta["extraction_source"])

    dead = extract(FIXTURE_DEAD)
    assert dead["pdp_availability"] == "unavailable", dead
    print("PASS  dead PDP detected as:", dead["pdp_availability"])

    shelf = extract(FIXTURE_WITH_SHELF_LIFE_TEXT)
    assert shelf["obs_shelf_life"] == "12 months", shelf
    print("PASS  shelf-life text pattern (synthetic, NOT validated against a "
          "real Nykaa page -- see module docstring):", shelf["obs_shelf_life"])

    print("\nAll parser self-tests passed "
          "(shelf-life extraction still needs real-page validation).")


# ===========================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="validate parser on fixtures")
    ap.add_argument("--worklist", default="nykaa_worklist.csv")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--failures", default=FAIL_DEFAULT)
    ap.add_argument("--limit", type=int, help="scrape only the first N (smoke test)")
    a = ap.parse_args()

    if a.selftest:
        selftest()
    else:
        asyncio.run(run(a.worklist, a.out, a.failures, a.limit))
