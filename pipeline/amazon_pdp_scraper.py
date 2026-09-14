#!/usr/bin/env python3
"""
Amazon PDP scraper for the Listing-QA sweep (observed content side) --
Amazon's counterpart to nykaa_pdp_scraper.py / myntra_pdp_scraper.py.

Reads amazon_worklist.csv and, for each ASIN, loads the public PDP and
extracts OBSERVED content: title, images, a composite description, ingredients,
price, shelf life, and a broken/delisted signal. Amazon product pages are
public (no login required to view), confirmed by Anika (2026-09-14).

URL FORMAT: https://www.amazon.in/dp/<asin> -- confirmed by Anika, ASIN is
the only variable part.

HEADLESS WORKS, BUT SCROLLING IS REQUIRED (confirmed 2026-09-14): unlike
Nykaa/Myntra, Amazon does NOT block headless Chromium outright -- the very
first real dump loaded cleanly with no CAPTCHA. But several sections
(the full "Important information" block -- ingredients/storage/safety/
directions -- and the detail-bullets table) were completely ABSENT from a
page captured right after domcontentloaded, even though they render fine in
a real browser. Checked Amazon's own page-telemetry JSON embedded in that
first dump: those feature divs are flagged "isp":1 (in-scroll-pane), i.e.
lazily loaded as the page is scrolled, not on initial paint. So this
scraper scrolls down in steps (see _scroll_page()) before reading the DOM,
same fix applied to dump_amazon_html.py.

BOT-CHECK FINDING (confirmed 2026-09-14, real worklist ASINs): a handful of
requests came back as Amazon's soft interstitial -- a page titled
"Amazon.in" with "Click the button below to continue shopping" and a form
posting to /errors_page/validateCaptcha. This is NOT a real "page broken"
signal (the real page underneath is fine -- a moment later, the very same
kind of request for a different ASIN succeeded cleanly, and a same-session
retry of a previously-challenged ASIN can also succeed) -- it looks like a
request-pacing/behavior heuristic, not a hard per-ASIN or per-session ban.
is_bot_check() detects it; _scrape_one() treats it as a transient failure
(retries via the normal MAX_RETRIES loop, a fresh page/context each time)
rather than ever writing it into the observed CSV as if it were real
product data -- misreading a bot-check as "delisted" would generate a false
P2 alert for a perfectly live product.

PAGE-NOT-FOUND FINDING (confirmed 2026-09-14, bogus ASIN B000000000): a
genuinely dead ASIN returns a clean, distinctive static page titled exactly
"Page Not Found", with body text "We're sorry. The Web address you entered
is not a functioning page on our site." is_page_not_found() checks for this
title exactly -- a real, easy, unambiguous signal, nothing like Amazon's
soft bot-check page (different title, different body text, no
validateCaptcha form).

SHELF-LIFE FINDING (confirmed 2026-09-14, real ASIN B0BK1V96Z4 / SKU
FC-KL-OK-040): Amazon has no single structured "shelf life" field like
Myntra's. Shelf-life-equivalent text shows up, when present at all, as free
text inside the "Important information" block, under a label that reads
"Storage:" -- e.g. "Storage: Pack for 3-4 uses. Use within 30days from Mfg
Date and store airtight in refrigerator." That "Storage:" label is
sometimes its own labeled sub-section, but was also observed run together
at the end of the "Directions:" paragraph on a real listing (a seller data-
entry quirk, not a scraper bug) -- so _split_important_information() works
on the whole block's flattened plain text and finds "Storage:" wherever it
falls, rather than assuming a fixed HTML structure per label. Many products
(e.g. the wooden comb, B09H33MF1P) have no Storage: text at all -- that's a
real, known-absent answer for a non-perishable item, not a scraper failure.
Whatever text is found is run through qa_diff.py's existing
shelf_life_match()/_DURATION_RE machinery unchanged -- it already handles
"30days" (no space, day-based) as well as "6 Months", converting both to
calendar-approximate days before comparing against D2C's stated shelf life,
so no new duration-parsing logic was needed here.

INGREDIENTS FINDING: same Important-information block usually carries an
"Ingredients:" labeled sub-section with a clean, real ingredient list (e.g.
"Orange, Kalonji, Papaya, Almond, Raw Milk, Yogurt"). Some listings instead
(or additionally) carry a structured "Active Ingredients" row in Amazon's
separate "Product overview" table (class="po-active_ingredients") -- kept as
a fallback when the Important-information block has no Ingredients: text.

DESCRIPTION FINDING: like Myntra, there's no single rich free-text
description field -- real descriptive content is spread across the
"About this item" feature bullets and the Important-information
sub-sections. The composite description joins both, deliberately EXCLUDING
the "Storage:" text (it's a duration claim, not descriptive content, and
would otherwise pollute the description-similarity score the same way
Myntra's numeric shelf-life fields were excluded) but keeping "Ingredients:"
in (same reasoning as Myntra -- qa_diff.ingredients_coverage() needs real
ingredient text to check against).

STOCK-STATUS FINDING: the actual rendered "In stock" / "Currently
unavailable" text lives in a small window right after id="availability" --
naive whole-page substring counts are unreliable here, since Amazon's page
carries several hidden/templated copies of stock-status phrases elsewhere
(confirmed against a real in-stock page that still contained 5 incidental
"Currently unavailable" substrings in unrelated hidden markup). Only text
inside that specific availability window is checked. Same design principle
as Flipkart/Nykaa/Myntra: a page that's simply out of stock is NOT
"broken" -- title/description/ingredients/shelf-life comparisons still run.
"""
import argparse
import asyncio
import csv
import html as htmllib
import os
import random
import re
import sys

OUT_COLS = [
    "nh_sku", "asin", "pdp_availability", "obs_title", "obs_image_count",
    "obs_image_urls", "obs_description_len", "obs_description_text",
    "obs_ingredients", "obs_price", "extraction_source", "obs_shelf_life",
    "obs_page_not_found", "scraped_at",
]

CONCURRENCY = 2  # kept low -- see the bot-check finding above; a lower
                 # concurrency + real delay between requests is the main
                 # lever available against a request-pacing-based check
MIN_DELAY, MAX_DELAY = 3.0, 6.0     # seconds between page loads, per worker
NAV_TIMEOUT_MS = 30000
MAX_RETRIES = 3   # one more than Nykaa/Myntra's 2 -- bot-check retries are
                   # expected to happen occasionally in normal operation,
                   # not just on genuine errors


# ===========================================================================
# Parsing -- pure functions, unit-testable without a browser (see selftest)
# ===========================================================================
def _clean(text):
    return " ".join((text or "").split())


def _strip_tags(html_fragment):
    """Strip tags AND fully unescape entities -- some real Amazon listings
    have seller-entered text that was HTML-escaped twice (a literal
    "&lt;/br&gt;" appearing in rendered text, confirmed on B0BK1V96Z4), so a
    single htmllib.unescape() leaves visible junk behind; unescaping twice
    cleans that up harmlessly for normal single-escaped text too."""
    text = re.sub(r"<[^>]+>", " ", html_fragment or "")
    text = htmllib.unescape(htmllib.unescape(text))
    return _clean(text)


def is_bot_check(html):
    """Amazon's soft anti-bot interstitial -- confirmed 2026-09-14 against
    real worklist ASINs (not a fixed/rare corner case -- happened on both
    the 2nd request to a same ASIN moments after a clean first load, and on
    a first request to a brand new ASIN). Distinct from is_page_not_found()
    below: different title, different body text, and this one has a form
    posting to /errors_page/validateCaptcha with a plain "Continue shopping"
    submit button (no actual puzzle to solve)."""
    return "errors_page/validateCaptcha" in html


def is_page_not_found(html):
    """A genuinely dead ASIN -- confirmed 2026-09-14 against a deliberately
    bogus ASIN (B000000000). Amazon returns a small static page titled
    exactly "Page Not Found" with distinctive body text; checked for both
    so an unrelated page that merely mentions "Page Not Found" in passing
    somewhere doesn't false-positive."""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL)
    title = _clean(m.group(1)) if m else ""
    return title == "Page Not Found" and "not a functioning page on our site" in html


def parse_title(html):
    m = re.search(r'id="productTitle"[^>]*>(.*?)</span>', html, re.DOTALL)
    return _strip_tags(m.group(1)) if m else ""


def parse_price(html):
    """First a-offscreen span is the current displayed price -- confirmed
    against real dumps; later a-offscreen occurrences further down the page
    are unrelated (subscribe-and-save tiers, per-100g unit-price call-outs,
    etc.)."""
    m = re.search(r'class="a-offscreen">([^<]+)</span>', html)
    return _clean(m.group(1)) if m else ""


def parse_feature_bullets(html):
    m = re.search(r'id="feature-bullets"(.*?)</ul>', html, re.DOTALL)
    if not m:
        return []
    items = re.findall(r"<li[^>]*>(.*?)</li>", m.group(1), re.DOTALL)
    bullets = [_strip_tags(i) for i in items]
    return [b for b in bullets if b and b.lower() != "about this item"]


# Recognized Important-information sub-headers -- covers every label seen
# across the real dumps collected 2026-09-14, plus a few common Amazon-India
# cosmetics ones added defensively (Legal Disclaimer, Caution, Indications)
# since not every category was sampled.
_IMPORTANT_INFO_LABELS = [
    "Safety Information", "Ingredients", "Directions", "Storage",
    "Legal Disclaimer", "Indications", "Caution", "Warnings",
]
_LABEL_SPLIT_RE = re.compile(
    r"(" + "|".join(re.escape(l) for l in _IMPORTANT_INFO_LABELS) + r")\s*:\s*",
    re.I,
)


def parse_important_information(html):
    """Returns {lowercase_label: text} for every recognized sub-section
    found inside the "Important information" block, worked out on the
    block's FLATTENED PLAIN TEXT (tags stripped, entities unescaped) rather
    than assuming each label is its own separate HTML element -- confirmed
    necessary 2026-09-14: on a real listing (B0BK1V96Z4), "Storage:" was NOT
    its own <h4> sub-section, it was run together at the end of the
    "Directions:" paragraph's text. Working on flattened text finds it
    either way. Returns {} if the block isn't present at all (lazy-loaded
    content that didn't render -- see the module docstring -- or a listing
    that genuinely has none of these, like a non-perishable item)."""
    m = re.search(
        r'<div id="important-information"(.*?)</div>\s*</div>\s*<div id="btfSubNavDesktop',
        html, re.DOTALL,
    )
    if not m:
        # Fall back to a looser bound in case the following sibling id ever
        # changes -- stop at the next big section boundary instead.
        m = re.search(r'<div id="important-information"(.*?)</div>\s*</div>\s*</div>', html, re.DOTALL)
    if not m:
        return {}
    plain = _strip_tags(m.group(1))
    plain = re.sub(r"^\s*Important information\s*", "", plain, flags=re.I)

    matches = list(_LABEL_SPLIT_RE.finditer(plain))
    if not matches:
        return {}
    sections = {}
    for i, lm in enumerate(matches):
        label = lm.group(1).strip().lower()
        start = lm.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(plain)
        sections[label] = plain[start:end].strip()
    return sections


def parse_active_ingredients_fallback(html):
    """Amazon's separate "Product overview" table sometimes carries a
    structured "Active Ingredients" row (class="po-active_ingredients") --
    confirmed on a real listing (B0B4VY3841) that had this table row but no
    Ingredients: text in Important information at all. Used only as a
    fallback when Important information has nothing."""
    m = re.search(
        r'po-active_ingredients"[^>]*>.*?<span class="a-size-base po-break-word">(.*?)</span>',
        html, re.DOTALL,
    )
    return _strip_tags(m.group(1)) if m else ""


def parse_gallery_images(html):
    """Amazon's real image gallery lives in a 'colorImages': {'initial':
    A.$.parseJSON('[...]')} JS variable -- confirmed 2026-09-14 against a
    real listing with several distinct photos. The single <img id=
    "landingImage" data-a-dynamic-image=...> attribute only ever lists
    resize/crop variants of ONE image, not the full gallery -- checked and
    ruled out as the primary source for that reason. Dedupes by
    physicalIdForMedia (the stable image id Amazon itself uses), prefers
    hiRes, falls back to large."""
    m = re.search(r"'colorImages'\s*:\s*\{\s*'initial'\s*:\s*A\.\$\.parseJSON\('(.*?)'\)\s*\}", html)
    if not m:
        return []
    raw = m.group(1)
    # The embedded JSON is itself inside a single-quoted JS string, with
    # internal double quotes left as-is and no escaped single quotes
    # observed in practice -- safe to parse directly.
    import json
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    seen = set()
    urls = []
    for item in items:
        pid = item.get("physicalIdForMedia") or item.get("hiRes") or item.get("large")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        url = item.get("hiRes") or item.get("large")
        if url:
            urls.append(url)
    return urls


def parse_availability(html):
    """Returns "in_stock", "unavailable", or "unknown". Checked only inside
    a small window right after id="availability" -- confirmed necessary
    2026-09-14: a naive whole-page substring count of "Currently
    unavailable" found 5 hits on a real page that was actually in stock
    (hidden/templated markup elsewhere on the page carries the same
    phrases). "unknown" deliberately does NOT mean unavailable -- treated
    the same as in_stock downstream (available for comparison), matching
    this project's resilience-first rule: never guess "broken" from a weak
    signal."""
    idx = html.find('id="availability"')
    if idx == -1:
        return "unknown"
    window = _strip_tags(html[idx:idx + 250]).lower()
    if "currently unavailable" in window or "out of stock" in window:
        return "unavailable"
    if "in stock" in window:
        return "in_stock"
    return "unknown"


def _build_description_text(bullets, sections):
    """Composite description: feature bullets + every Important-information
    sub-section EXCEPT "storage" (a duration claim, not descriptive content
    -- excluding it mirrors Myntra's exact precedent of excluding its
    numeric shelf-life keys from the composite description). "ingredients"
    is deliberately KEPT IN, same reasoning as Myntra: qa_diff.py's
    ingredients_coverage() needs real ingredient text somewhere in the
    description to check D2C's ingredient list against. Sorted by label for
    determinism."""
    parts = list(bullets)
    for label in sorted(sections):
        if label == "storage":
            continue
        text = sections[label]
        if text:
            parts.append(text)
    return " ".join(p for p in parts if p)


def extract(html):
    """Merge every parser above into a single observed record. Bot-check
    detection is NOT handled here -- that's a transient scrape condition,
    handled by the caller (_scrape_one) via retry, never written as if it
    were real product data. Page-not-found IS handled here since it's a
    genuine, stable signal about the listing itself."""
    if is_page_not_found(html):
        return {
            "pdp_availability": "page_not_found",
            "obs_title": "", "obs_image_count": 0, "obs_image_urls": "",
            "obs_description_len": 0, "obs_description_text": "",
            "obs_ingredients": "", "obs_price": "",
            "extraction_source": "none", "obs_shelf_life": "",
            "obs_page_not_found": True,
        }

    title = parse_title(html)
    if not title:
        return {
            "pdp_availability": "no_content",
            "obs_title": "", "obs_image_count": 0, "obs_image_urls": "",
            "obs_description_len": 0, "obs_description_text": "",
            "obs_ingredients": "", "obs_price": "",
            "extraction_source": "none", "obs_shelf_life": "",
            "obs_page_not_found": False,
        }

    availability = parse_availability(html)
    images = parse_gallery_images(html)
    bullets = parse_feature_bullets(html)
    sections = parse_important_information(html)
    ingredients = sections.get("ingredients") or parse_active_ingredients_fallback(html)
    shelf_life = sections.get("storage", "")
    description = _build_description_text(bullets, sections)
    price = parse_price(html)

    pdp_availability = "unavailable" if availability == "unavailable" else "available"

    return {
        "pdp_availability": pdp_availability,
        "obs_title": title,
        "obs_image_count": len(images),
        "obs_image_urls": "|".join(images),
        "obs_description_len": len(description),
        "obs_description_text": description,
        "obs_ingredients": ingredients,
        "obs_price": price,
        "extraction_source": "dom",
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
        return {(r["nh_sku"], r["asin"]) for r in csv.DictReader(f) if r.get("asin")}


async def _scroll_page(page):
    """Scroll the full page height in steps, pausing each time, so every
    lazily-loaded ("in-scroll-pane") feature div gets a chance to fire its
    content -- see the module docstring. Identical approach to
    dump_amazon_html.py, which is what confirmed this fix works."""
    prev_height = 0
    for _ in range(20):
        height = await page.evaluate("document.body.scrollHeight")
        if height == prev_height:
            break
        prev_height = height
        await page.evaluate("window.scrollBy(0, document.body.scrollHeight / 10)")
        await page.wait_for_timeout(500)


async def _scrape_one(context, row, out_writer, fail_writer, lock):
    import datetime
    asin, nh, url = row["asin"], row["nh_sku"], row["pdp_url"]
    for attempt in range(1, MAX_RETRIES + 1):
        page = await context.new_page()
        try:
            await page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            html = await page.content()
            if is_bot_check(html):
                # Not a real signal about the listing -- see the module
                # docstring's BOT-CHECK FINDING. Treat exactly like any
                # other transient failure: close this page, retry with a
                # fresh one (a fresh page/context sometimes succeeds where
                # the previous one didn't, confirmed against real ASINs).
                raise RuntimeError("amazon_bot_check")
            await _scroll_page(page)
            html = await page.content()
            rec = extract(html)
            rec.update({
                "nh_sku": nh, "asin": asin,
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
                    fail_writer.writerow({"nh_sku": nh, "asin": asin, "url": url,
                                          "error": repr(e)[:200]})
                return False
            await asyncio.sleep(3 * attempt + random.uniform(0, 2))


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
    todo = [r for r in work if (r["nh_sku"], r["asin"]) not in done]
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
    fail_writer = csv.DictWriter(fail_f, fieldnames=["nh_sku", "asin", "url", "error"])
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
        # headless=True by default -- confirmed 2026-09-14 that Amazon does
        # NOT block headless Chromium outright (unlike Nykaa/Myntra). The
        # real operational issue here is the bot-check interstitial (see
        # module docstring), which --headed does not appear to prevent any
        # more reliably than headless based on the samples collected so
        # far -- CONCURRENCY/MIN_DELAY/MAX_DELAY and the retry loop are the
        # actual mitigation. --headed is still offered as an escape hatch.
        browser = await p.chromium.launch(headless=not headed)
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
# Self-test (no browser, no network) -- built from real confirmed field
# structure, see the docstrings above for exactly which real ASIN/finding
# each fixture reproduces.
# ===========================================================================
FIXTURE_MASK_WITH_STORAGE = """
<html><head><title>Buy Nat Habit Orange &amp; Kalonji Ksheer Face Pack Online</title></head>
<body>
<span id="productTitle" class="a-size-large product-title-word-break"> Nat Habit Orange &amp; Kalonji Face Pack </span>
<div id="availability"><span class="a-size-medium a-color-success"> In stock </span></div>
<span class="a-price aok-align-center"><span class="a-offscreen">₹222.00</span></span>
<script>var x = {'colorImages': { 'initial': A.$.parseJSON('[{"hiRes":"https://m.media-amazon.com/images/I/one.jpg","large":"https://m.media-amazon.com/images/I/one_l.jpg","physicalIdForMedia":"one"},{"hiRes":"https://m.media-amazon.com/images/I/two.jpg","large":"https://m.media-amazon.com/images/I/two_l.jpg","physicalIdForMedia":"two"}]') }};</script>
<div id="feature-bullets"><ul><li><span class="a-list-item"> About this item </span></li><li><span class="a-list-item"> QUICK FACE MASK for glow </span></li></ul></div>
<div id="important-information" class="a-section a-spacing-extra-large bucket">  <h2>Important information</h2>   <div class="a-section content">    <h4>Safety Information:</h4>    <p></p><p>May tingle at first.</p><p></p>  </div>  <div class="a-section content">    <h4>Ingredients:</h4>    <p></p><p>Orange, Kalonji, Papaya, Almond, Raw Milk, Yogurt</p><p></p>  </div>  <div class="a-section content">    <h4>Directions:</h4>    <p></p><p>Apply thick layer.&lt;/br&gt;Storage: Pack for 3-4 uses. Use within 30days from Mfg Date and store airtight in refrigerator.</p><p></p>  </div>  </div>                                </div><div id="btfSubNavDesktopCopy">
</body></html>
"""

FIXTURE_COMB_NO_SHELF_LIFE = """
<html><head><title>Buy Nat Habit Kacchi Neem Wooden Comb Online</title></head>
<body>
<span id="productTitle" class="a-size-large product-title-word-break"> Nat Habit Kacchi Neem Wooden Comb </span>
<div id="availability"><span class="a-size-medium a-color-success"> In stock </span></div>
<span class="a-price aok-align-center"><span class="a-offscreen">&#8377;195.00</span></span>
<script>var x = {'colorImages': { 'initial': A.$.parseJSON('[{"hiRes":"https://m.media-amazon.com/images/I/comb.jpg","large":"https://m.media-amazon.com/images/I/comb_l.jpg","physicalIdForMedia":"comb1"}]') }};</script>
<div id="feature-bullets"><ul><li><span class="a-list-item"> About this item </span></li><li><span class="a-list-item"> WOODEN comb for detangling </span></li></ul></div>
<div id="important-information" class="a-section a-spacing-extra-large bucket">  <h2>Important information</h2>   <div class="a-section content">    <h4>Directions:</h4>    <p></p><p>Ensure teeth touch scalp while combing.</p><p></p>  </div>  </div>                                </div><div id="btfSubNavDesktopCopy">
</body></html>
"""

# Reproduces the real bot-check interstitial (2026-09-14, real worklist
# ASINs) -- extract() itself is never called on this in production (caught
# earlier by is_bot_check() in _scrape_one), but is_bot_check() is tested
# directly against it here.
FIXTURE_BOT_CHECK = """
<html><head><title dir="ltr">Amazon.in</title></head><body>
<h4>Click the button below to continue shopping</h4>
<form method="get" action="/errors_page/validateCaptcha" name="">
<button type="submit" class="a-button-text" alt="Continue shopping">Continue shopping</button>
</form>
</body></html>
"""

# Reproduces the real dead-ASIN page (2026-09-14, bogus ASIN B000000000).
FIXTURE_PAGE_NOT_FOUND = """
<html><head><title>Page Not Found</title></head>
<body>We're sorry. The Web address you entered is not a functioning page on our site.</body></html>
"""

# Amazon's separate "Product overview" table sometimes carries Active
# Ingredients even when Important information has no Ingredients: text at
# all -- confirmed on a real listing (B0B4VY3841). Fallback path.
FIXTURE_PO_TABLE_INGREDIENTS_FALLBACK = """
<html><head><title>Buy Nat Habit Masoor Rub Bath Ubtan Online</title></head>
<body>
<span id="productTitle" class="a-size-large product-title-word-break"> Nat Habit Masoor Rub Bath Ubtan </span>
<div id="availability"><span class="a-size-medium a-color-success"> In stock </span></div>
<span class="a-price aok-align-center"><span class="a-offscreen">&#8377;342.00</span></span>
<script>var x = {'colorImages': { 'initial': A.$.parseJSON('[{"hiRes":"https://m.media-amazon.com/images/I/ubtan.jpg","large":"https://m.media-amazon.com/images/I/ubtan_l.jpg","physicalIdForMedia":"ubtan1"}]') }};</script>
<div id="feature-bullets"><ul><li><span class="a-list-item"> About this item </span></li><li><span class="a-list-item"> QUICK 2 MIN BODY SCRUB </span></li></ul></div>
<tr class="a-spacing-small po-active_ingredients" role="listitem"> <td class="a-span3" role="presentation">     <span class="a-size-base a-text-bold">Active Ingredients</span>   </td> <td class="a-span9" role="presentation">    <span class="a-size-base po-break-word">Masoor Dal, Besan, Yogurt</span>   </td> </tr>
</body></html>
"""

FIXTURE_OUT_OF_STOCK = """
<html><head><title>Buy Nat Habit Test Product Online</title></head>
<body>
<span id="productTitle" class="a-size-large product-title-word-break"> Nat Habit Test Product </span>
<div id="availability"><span class="a-size-medium a-color-price"> Currently unavailable. </span></div>
<div id="feature-bullets"><ul><li><span class="a-list-item"> About this item </span></li><li><span class="a-list-item"> A test product </span></li></ul></div>
</body></html>
"""

# Real in-stock page carries several INCIDENTAL "Currently unavailable"
# substrings elsewhere in hidden/templated markup -- confirmed 2026-09-14
# (5 occurrences on a genuinely in-stock page). This fixture reproduces
# that trap: parse_availability() must only look inside the id="availability"
# window, not count substrings anywhere on the page.
FIXTURE_INSTOCK_WITH_MISLEADING_TEXT = """
<html><head><title>Buy Nat Habit Test Product Online</title></head>
<body>
<span id="productTitle" class="a-size-large product-title-word-break"> Nat Habit Test Product </span>
<div id="availability"><span class="a-size-medium a-color-success"> In stock </span></div>
<div id="feature-bullets"><ul><li><span class="a-list-item"> About this item </span></li><li><span class="a-list-item"> A test product </span></li></ul></div>
<div style="display:none">unrelated template markup padding padding padding padding padding padding padding padding padding padding padding padding padding padding padding padding padding padding padding padding padding padding padding padding padding padding padding padding Currently unavailable Currently unavailable Currently unavailable Currently unavailable Currently unavailable</div>
</body></html>
"""


def selftest():
    mask = extract(FIXTURE_MASK_WITH_STORAGE)
    assert mask["pdp_availability"] == "available", mask
    assert mask["obs_title"] == "Nat Habit Orange & Kalonji Face Pack", mask
    assert mask["obs_ingredients"] == "Orange, Kalonji, Papaya, Almond, Raw Milk, Yogurt", mask
    assert mask["obs_shelf_life"] == "Pack for 3-4 uses. Use within 30days from Mfg Date and store airtight in refrigerator.", mask
    assert "Storage" not in mask["obs_description_text"] and "30days" not in mask["obs_description_text"], mask
    assert "Orange, Kalonji" in mask["obs_description_text"], mask
    assert mask["obs_image_count"] == 2, mask
    assert mask["obs_price"] == "₹222.00", mask
    print("PASS  mask with Storage: embedded inside Directions text (the real "
          "structure found on B0BK1V96Z4) -- shelf life extracted, kept out of "
          "the description, ingredients kept in")

    comb = extract(FIXTURE_COMB_NO_SHELF_LIFE)
    assert comb["pdp_availability"] == "available", comb
    assert comb["obs_shelf_life"] == "", comb
    assert comb["obs_ingredients"] == "", comb
    print("PASS  non-perishable product (comb) correctly has no shelf life/"
          "ingredients -- a real known-absent answer, not a scraper failure")

    assert is_bot_check(FIXTURE_BOT_CHECK) is True
    assert is_bot_check(FIXTURE_MASK_WITH_STORAGE) is False
    print("PASS  bot-check interstitial detected via validateCaptcha form "
          "action, and correctly NOT flagged on a real product page")

    nf = extract(FIXTURE_PAGE_NOT_FOUND)
    assert nf["pdp_availability"] == "page_not_found", nf
    assert nf["obs_page_not_found"] is True, nf
    print("PASS  page_not_found correctly read from the real dead-ASIN page "
          "title+body text (bogus ASIN B000000000, 2026-09-14)")

    fallback = extract(FIXTURE_PO_TABLE_INGREDIENTS_FALLBACK)
    assert fallback["obs_ingredients"] == "Masoor Dal, Besan, Yogurt", fallback
    print("PASS  Active Ingredients fallback read from the po-active_ingredients "
          "table when Important information has no Ingredients: text")

    oos = extract(FIXTURE_OUT_OF_STOCK)
    assert oos["pdp_availability"] == "unavailable", oos
    print("PASS  Currently unavailable correctly read from the id=\"availability\" window")

    misleading = extract(FIXTURE_INSTOCK_WITH_MISLEADING_TEXT)
    assert misleading["pdp_availability"] == "available", misleading
    print("PASS  incidental 'Currently unavailable' text elsewhere on a real "
          "in-stock page does NOT cause a false unavailable read (the real "
          "trap this fixture reproduces -- see its comment)")

    for label, rec in [("mask", mask), ("comb", comb), ("fallback", fallback), ("oos", oos), ("misleading", misleading)]:
        assert rec["obs_page_not_found"] is False, (label, rec)
    print("PASS  obs_page_not_found is False on every other fixture (no false positives)")

    print("\nAll parser self-tests passed.")


# ===========================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="validate parser on fixtures")
    ap.add_argument("--worklist", default="amazon_worklist.csv")
    ap.add_argument("--out", default="observed_amazon_latest.csv")
    ap.add_argument("--failures", default="amazon_failures.csv")
    ap.add_argument("--limit", type=int, help="scrape only the first N (smoke test)")
    ap.add_argument("--headed", action="store_true",
                     help="show the actual browser window instead of running hidden -- "
                          "not confirmed to help against the bot-check (see run()'s "
                          "comment), but offered as an escape hatch to test")
    a = ap.parse_args()

    if a.selftest:
        selftest()
    else:
        asyncio.run(run(a.worklist, a.out, a.failures, a.limit, headed=a.headed))
