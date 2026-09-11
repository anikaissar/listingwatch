#!/usr/bin/env python3
"""
Nykaa PDP scraper for the Listing-QA sweep (observed content side) --
Nykaa's counterpart to flipkart_pdp_scraper.py.

Reads nykaa_worklist.csv and, for each Nykaa product ID, loads the public PDP
and extracts OBSERVED content: title, image count, description, price, and a
best-effort shelf-life/expiry read. Nykaa product pages are public (no login
required to view), unlike Flipkart.

Extraction strategy (same resilience-first approach as the Flipkart scraper):
  0. Nykaa's own window.__PRELOADED_STATE__ blob  -> page_not_found, expiry
  1. JSON-LD  <script type="application/ld+json"> Product block  -> name, image[], description, price
  2. og:/meta tags                                              -> title, image, description
  3. Whole-page text regex scan for shelf-life/expiry phrasing   -> fallback only,
     used only when step 0's state blob isn't present at all

PAGE-NOT-FOUND FINDING (confirmed 2026-09-11, product 10346740 / SKU
BC-SM-MNS-120): this SKU is listed Active in our own product master, but
Nykaa itself returns a 404 for it (appReducer.statusCode:404,
productPage.isNotFound:true, productPage.product:null). is_page_not_found()
reads this directly -- a real "page broken/removed" signal, distinct from
"unavailable" (page exists, out of stock) and worth a separate
pdp_availability value: "page_not_found". obs_page_not_found is True/False
on every row so downstream diffing doesn't need to string-match the enum.

SHELF-LIFE FINDING (confirmed at scale, 2026-09-11 -- 45/45 real products
sampled across two test rounds): Nykaa's own product page embeds a full
state blob in that same `window.__PRELOADED_STATE__ = {...}` script tag,
and it has exactly one `expiry` field, at `productPage.product.expiry` --
Nykaa's own structured field for shelf-life/expiry, authoritative and far
more reliable than guessing from rendered text (which is what the old
_extract_shelf_life_text() regex scan did). parse_preloaded_state() reads
this field directly.

However: across all 45 real Nykaa products sampled so far, this field was
`null` on every single one. That's not a scraper bug -- Nykaa's own catalog
simply has no expiry value configured for any Nat Habit listing checked.
obs_shelf_life will come back blank for most/all Nykaa rows, and that in
itself is worth surfacing to whoever manages the Nykaa listings as an
action item (Nykaa lets sellers set this field; it's just not populated),
separate from anything qa_diff.py can compute -- there is currently no
Flipkart-style "shelf life mismatch" tier possible for Nykaa via scraping.
Keep the old free-text regex scan as a fallback only for the rare case a
page doesn't have the __PRELOADED_STATE__ blob at all -- when the blob IS
present and expiry is null, treat that as a real, known-blank answer rather
than falling back to fuzzy text matching (the JSON-LD availability bug
already showed that text heuristics on this site produce false positives).

Your own nykaa_worklist.csv already carries a `master_shelf_life_days`
column (from your internal product master, e.g. 360 days) -- that's your
ground truth. obs_shelf_life is what Nykaa's page itself claims, which may
legitimately be blank; the interesting QA signal may end up being "master
says X days but Nykaa has nothing configured" rather than a mismatch in
values.

NETWORK: the live scrape hits nykaa.com, unreachable from this sandbox. Run
in your environment. Use --selftest to validate the parser against bundled
fixtures (JSON-LD/meta/__PRELOADED_STATE__ synthetic fixtures -- the actual
field paths and behavior were confirmed against real scraped HTML dumps,
see the findings above).
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
    "extraction_source", "obs_shelf_life", "obs_page_not_found", "scraped_at",
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
                availability = offers.get("availability", "") if isinstance(offers, dict) else ""
                return {
                    "title": _clean(c.get("name", "")),
                    "images": [i for i in imgs if i],
                    "description": _clean(c.get("description", "")),
                    "price": str(price),
                    "availability": str(availability),
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
    """Best-effort only, used only as a fallback when the page has no
    __PRELOADED_STATE__ blob to read (see parse_preloaded_state and the
    module docstring's SHELF-LIFE FINDING). Strips tags to plain text first
    so phrasing split across nested elements still matches."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    for pat in _SHELF_LIFE_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return _clean(m.group(1))
    return ""


_PRELOADED_STATE_RE = re.compile(r"window\.__PRELOADED_STATE__\s*=\s*")


def _parse_preloaded_state_raw(html):
    """Locate and JSON-parse Nykaa's own embedded
    `window.__PRELOADED_STATE__ = {...}` blob. Returns the parsed dict, or
    None if the blob isn't present or doesn't parse. Both
    parse_preloaded_state() (shelf-life) and is_page_not_found() (page
    broken/removed) read from this same parsed blob rather than each
    re-scanning the page."""
    m = _PRELOADED_STATE_RE.search(html)
    if not m or m.end() >= len(html) or html[m.end()] != "{":
        return None

    # Brace-balance scan to find the matching closing brace, respecting
    # quoted strings (which may contain escaped quotes/braces).
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


def parse_preloaded_state(html, state=None):
    """Read productPage.product.expiry from Nykaa's own
    __PRELOADED_STATE__ blob -- confirmed (2026-09-11, against two real
    Nykaa PDPs -- a wooden comb and a face mask) to be the one real
    structured field Nykaa uses for shelf-life/expiry data. This is the
    authoritative source; see the module docstring's SHELF-LIFE FINDING for
    why it should NOT be treated the same as a missing value.

    Pass `state` (an already-parsed blob from _parse_preloaded_state_raw)
    to avoid re-parsing when the caller also needs is_page_not_found().

    Returns a dict: {"found": bool, "expiry": str}.
      - found=False means the blob wasn't present or didn't parse -- caller
        should fall back to extract_shelf_life_text().
      - found=True, expiry="" means the blob WAS found and its expiry field
        is null/blank -- a real answer, not a signal to fall back.
    """
    data = state if state is not None else _parse_preloaded_state_raw(html)
    if data is None:
        return {"found": False, "expiry": ""}
    expiry = (data.get("productPage") or {}).get("product", {}).get("expiry")
    return {"found": True, "expiry": _clean(str(expiry)) if expiry else ""}


def is_page_not_found(html, state=None):
    """Read Nykaa's own productPage.isNotFound / appReducer.statusCode==404
    fields -- confirmed (2026-09-11) against a real dead listing
    (BC-SM-MNS-120, product 10346740: appReducer.statusCode was 404 and
    productPage.product was null/None, even though our own product master
    lists this SKU as Active). This is a much more reliable "page broken"
    signal than inferring it from JSON-LD/meta simply being absent.

    Pass `state` (an already-parsed blob from _parse_preloaded_state_raw)
    to avoid re-parsing when the caller also needs parse_preloaded_state().

    Returns True/False, or None if the state blob wasn't found at all (in
    which case the caller should fall back to other signals, e.g. no title
    anywhere -> "no_content")."""
    data = state if state is not None else _parse_preloaded_state_raw(html)
    if data is None:
        return None
    pp = data.get("productPage") or {}
    if pp.get("isNotFound"):
        return True
    if (data.get("appReducer") or {}).get("statusCode") == 404:
        return True
    return False


def detect_unavailable(html):
    """Fallback heuristic, used only when the JSON-LD Product block has no
    offers.availability field to go on (see extract()). Confirmed against a
    real 3-row test run that this text-substring approach alone gives false
    positives -- all 3 real, in-stock, fully-priced products got flagged
    "unavailable" by it, most likely because a phrase like "out of stock"
    appears somewhere in the page's inline JS/template code (e.g. a hidden
    "notify me" widget or a related/recommended item) even though it's not
    shown for *this* product. Real availability is far more reliably read
    from JSON-LD's own offers.availability field (schema.org InStock /
    OutOfStock) -- extract() only falls back to this function when that
    field is missing entirely."""
    low = html.lower()
    signals = [
        "product is currently unavailable",
        "page not found",
        "sorry, this product",
        "we couldn't find that page",
    ]
    return any(s in low for s in signals)


# Nykaa's own product-image CDN path. JSON-LD only lists ONE image per
# product on Nykaa (confirmed: a real page with 11 images in its visible
# gallery still had just 1 in JSON-LD) -- unlike Flipkart, where JSON-LD
# carries the full set. This regex scans the whole page for every image URL
# under this CDN path as a best-effort stand-in for the real gallery count.
# CAVEAT (unvalidated at scale): this could also pick up "you may also like"/
# recommended-product thumbnails elsewhere on the page, which would inflate
# the count above the true gallery size -- treat obs_image_count as an
# approximation until checked against a real product's actual gallery count.
_GALLERY_IMG_RE = re.compile(
    r'https://images-static\.nykaa\.com/media/catalog/product/[^\s"\'\\)]+',
    re.IGNORECASE,
)


def extract_gallery_images(html):
    seen = []
    for m in _GALLERY_IMG_RE.finditer(html):
        url = m.group(0)
        if url not in seen:
            seen.append(url)
    return seen


def extract(html):
    """Merge strategies into a single observed record.

    Check order: page-not-found (a Nykaa-confirmed structural signal) wins
    over everything else, since a 404'd listing has no meaningful title/
    image/availability data to report -- see is_page_not_found()'s docstring
    for the real dead-listing case this was found against."""
    raw_state = _parse_preloaded_state_raw(html)
    not_found = is_page_not_found(html, state=raw_state)

    j = parse_jsonld(html)
    m = parse_meta(html)
    primary = j if j.get("title") else m

    if not_found:
        return {
            "pdp_availability": "page_not_found",
            "obs_title": primary.get("title", ""),
            "obs_image_count": 0,
            "obs_image_urls": "",
            "obs_description_len": 0,
            "obs_description_text": "",
            "obs_price": "",
            "extraction_source": primary.get("source", "none"),
            "obs_shelf_life": "",
            "obs_page_not_found": True,
        }

    if not primary.get("title"):
        return {
            "pdp_availability": "no_content",
            "obs_title": "", "obs_image_count": 0, "obs_image_urls": "",
            "obs_description_len": 0,
            "obs_description_text": "", "obs_price": "", "extraction_source": "none",
            "obs_shelf_life": "",
            "obs_page_not_found": False,
        }

    # Prefer JSON-LD's own offers.availability signal (schema.org InStock /
    # OutOfStock) over free-text scanning -- see detect_unavailable()'s
    # docstring for why the text heuristic alone gave false positives.
    availability_field = (j.get("availability") or "").lower()
    if availability_field:
        unavailable = "outofstock" in availability_field.replace(" ", "")
    else:
        unavailable = detect_unavailable(html)

    gallery = extract_gallery_images(html)
    images = gallery if len(gallery) > len(j.get("images") or []) else (j.get("images") or m.get("images") or [])

    if unavailable:
        return {
            "pdp_availability": "unavailable",
            "obs_title": primary.get("title", ""),
            "obs_image_count": len(images),
            "obs_image_urls": "|".join(images),
            "obs_description_len": len(primary.get("description", "")),
            "obs_description_text": primary.get("description", ""),
            "obs_price": primary.get("price", ""),
            "extraction_source": primary.get("source", "none"),
            "obs_shelf_life": "",
            "obs_page_not_found": False,
        }
    desc = j.get("description") or m.get("description") or ""
    price = j.get("price") or m.get("price") or ""

    # Prefer Nykaa's own structured expiry field over free-text guessing --
    # see parse_preloaded_state()'s docstring and the module docstring's
    # SHELF-LIFE FINDING. Only fall back to the text scan when the state
    # blob itself couldn't be found/parsed at all. Reuse raw_state (already
    # parsed above for the not-found check) instead of re-parsing.
    state = parse_preloaded_state(html, state=raw_state)
    shelf_life = state["expiry"] if state["found"] else extract_shelf_life_text(html)

    return {
        "pdp_availability": "available",
        "obs_title": primary["title"],
        "obs_image_count": len(images),
        "obs_image_urls": "|".join(images),
        "obs_description_len": len(desc),
        "obs_description_text": desc,
        "obs_price": price,
        "extraction_source": primary["source"],
        "obs_shelf_life": shelf_life,
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
        ok = await _scrape_one(context, row, out_writer, fail_writer, lock)
        if not ok:
            counter["failed"] += 1
        counter["done"] += 1
        if counter["done"] % 25 == 0:
            print(f"  ...{counter['done']}/{counter['total']} scraped", file=sys.stderr)
        await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


async def run(worklist_path, out_path, fail_path, limit=None, headed=False):
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
    counter = {"done": 0, "total": len(todo), "failed": 0}

    async with async_playwright() as p:
        # --disable-http2: nykaa.com's servers reset the connection with
        # net::ERR_HTTP2_PROTOCOL_ERROR on every single request from a
        # default headless Chromium launch (confirmed against a real 10-row
        # test run -- 10/10 failures, all this exact error). Forcing HTTP/1.1
        # was tried as a fix but a follow-up 10-row run still failed 10/10
        # (first attempt: same HTTP2 error; retry: a plain 30s timeout with
        # nothing loading at all) -- pointing at Nykaa detecting and blocking
        # the headless browser itself, not just an HTTP/2 quirk. `headed`
        # runs a real, visible browser window instead, which some anti-bot
        # systems treat differently than a headless one; use --headed to
        # test this theory (needs a real desktop session, not useful for an
        # unattended Task Scheduler run if it turns out to be required).
        browser = await p.chromium.launch(headless=not headed, args=["--disable-http2"])
        ctx_kwargs = {
            "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0 Safari/537.36"),
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
<html><head>
<meta property="og:title" content="Nat Habit Some Discontinued Product">
</head>
<body><div>Sorry, this product is currently unavailable and has been discontinued.</div></body></html>
"""

# Reproduces the real bug found in a 3-row production test: all 3 were real,
# in-stock, fully-priced products, but got flagged "unavailable" by the old
# text-only heuristic -- almost certainly because a phrase like "out of
# stock" appears somewhere in the page's inline JS/templates (e.g. a hidden
# widget for a *different*, related item) even though this product itself is
# in stock. JSON-LD's own offers.availability field must win over that.
FIXTURE_INSTOCK_WITH_MISLEADING_TEXT = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Nat Habit Comb Real Available Product",
 "image":["https://nykaa/comb.jpg"],
 "description":"A real comb.",
 "offers":{"@type":"Offer","price":"199","availability":"http://schema.org/InStock"}}
</script></head><body>
<div style="display:none">Related item: XYZ is out of stock right now.</div>
</body></html>
"""

FIXTURE_OUTOFSTOCK_JSONLD = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Nat Habit Discontinued Serum",
 "image":["https://nykaa/serum2.jpg"],
 "description":"Discontinued serum.",
 "offers":{"@type":"Offer","price":"0","availability":"http://schema.org/OutOfStock"}}
</script></head><body></body></html>
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

# Real Nykaa pages carry a window.__PRELOADED_STATE__ blob with a
# productPage.product.expiry field -- confirmed (2026-09-11) against two
# real PDPs. When it's populated, it should win outright.
FIXTURE_PRELOADED_STATE_WITH_EXPIRY = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Nat Habit Onion Hair Oil 100ml",
 "image":["https://nykaa/onion.jpg"],
 "description":"Onion hair oil.",
 "offers":{"@type":"Offer","price":"299","availability":"http://schema.org/InStock"}}
</script>
<script>window.__PRELOADED_STATE__ = {"productPage":{"product":{"expiry":"12 Months from date of manufacture","sku":"NATHA0001"}}};</script>
</head><body></body></html>
"""

# When the state blob IS found but its expiry is null, that's a real, known
# answer (Nykaa has no expiry configured for this listing) -- NOT a signal
# to fall back to free-text scanning. This fixture deliberately also
# contains misleading shelf-life-looking text elsewhere on the page to
# prove the null stays null rather than picking that up.
FIXTURE_PRELOADED_STATE_NULL_EXPIRY = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Nat Habit Kacchi Neem Wooden Comb",
 "image":["https://nykaa/comb.jpg"],
 "description":"Wooden comb.",
 "offers":{"@type":"Offer","price":"195","availability":"http://schema.org/InStock"}}
</script>
<script>window.__PRELOADED_STATE__ = {"productPage":{"product":{"expiry":null,"sku":"NATHA0002"}}};</script>
</head><body>
<div style="display:none">Unrelated widget text: Shelf Life: 24 months (not this product's real data)</div>
</body></html>
"""


# Reproduces the real dead-listing case (2026-09-11): product 10346740
# (BC-SM-MNS-120), listed as Active in our own product master, actually
# returns a 404 from Nykaa -- appReducer.statusCode:404, productPage.product
# null. No JSON-LD/meta at all, same as any other empty page, but this
# structured signal lets us call it out distinctly as "page broken/removed"
# rather than lumping it in with "no_content" (which could also mean a
# transient scrape hiccup).
FIXTURE_PAGE_NOT_FOUND = """
<html><head>
<script>window.__PRELOADED_STATE__ = {"appReducer":{"statusCode":404},"productPage":{"isNotFound":true,"product":null}};</script>
</head><body></body></html>
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
    print("PASS  dead PDP (no JSON-LD, text fallback) detected as:", dead["pdp_availability"])

    misleading = extract(FIXTURE_INSTOCK_WITH_MISLEADING_TEXT)
    assert misleading["pdp_availability"] == "available", misleading
    print("PASS  JSON-LD availability:InStock overrides misleading incidental "
          "'out of stock' text elsewhere on the page (the real bug this fixture "
          "reproduces -- see its comment)")

    oos = extract(FIXTURE_OUTOFSTOCK_JSONLD)
    assert oos["pdp_availability"] == "unavailable", oos
    print("PASS  JSON-LD availability:OutOfStock correctly read from the "
          "structured field")

    shelf = extract(FIXTURE_WITH_SHELF_LIFE_TEXT)
    assert shelf["obs_shelf_life"] == "12 months", shelf
    print("PASS  shelf-life text fallback (no __PRELOADED_STATE__ blob "
          "present, so falls back to text scan):", shelf["obs_shelf_life"])

    with_expiry = extract(FIXTURE_PRELOADED_STATE_WITH_EXPIRY)
    assert with_expiry["obs_shelf_life"] == "12 Months from date of manufacture", with_expiry
    print("PASS  __PRELOADED_STATE__ expiry field read directly (real "
          "Nykaa structured field, confirmed against live pages 2026-09-11):",
          with_expiry["obs_shelf_life"])

    null_expiry = extract(FIXTURE_PRELOADED_STATE_NULL_EXPIRY)
    assert null_expiry["obs_shelf_life"] == "", null_expiry
    print("PASS  __PRELOADED_STATE__ found with expiry:null stays blank "
          "(a real known-absent answer) instead of falling back to "
          "misleading text elsewhere on the page")

    not_found = extract(FIXTURE_PAGE_NOT_FOUND)
    assert not_found["pdp_availability"] == "page_not_found", not_found
    assert not_found["obs_page_not_found"] is True, not_found
    print("PASS  page_not_found correctly read from productPage.isNotFound/"
          "appReducer.statusCode (the real dead-listing case found "
          "2026-09-11 against product 10346740, listed Active in our own "
          "master but 404 on Nykaa)")

    for label, rec in [
        ("live", live), ("meta", meta), ("dead", dead),
        ("misleading", misleading), ("oos", oos), ("shelf", shelf),
        ("with_expiry", with_expiry), ("null_expiry", null_expiry),
    ]:
        assert rec["obs_page_not_found"] is False, (label, rec)
    print("PASS  obs_page_not_found is False on every other fixture (no "
          "false positives)")

    print("\nAll parser self-tests passed.")


# ===========================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="validate parser on fixtures")
    ap.add_argument("--worklist", default="nykaa_worklist.csv")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--failures", default=FAIL_DEFAULT)
    ap.add_argument("--limit", type=int, help="scrape only the first N (smoke test)")
    ap.add_argument("--headed", action="store_true",
                     help="show the actual browser window instead of running hidden -- "
                          "try this if every row fails (see run()'s comment)")
    a = ap.parse_args()

    if a.selftest:
        selftest()
    else:
        asyncio.run(run(a.worklist, a.out, a.failures, a.limit, headed=a.headed))
