#!/usr/bin/env python3
"""
Flipkart PDP scraper for the Listing-QA sweep (observed content side).

Reads flipkart_worklist.csv and, for each FSN, loads the public PDP and extracts
OBSERVED content: title, image count, description presence/length. It also records
a PDP-availability signal (page loaded with a real product vs. 404 / "not
available") as a *proxy* live signal. Authoritative live/suppressed still comes
from the seller panel and merges in later on nh_sku+fsn.

Extraction strategy (resilience-first, because Flipkart obfuscates DOM classes):
  1. JSON-LD  <script type="application/ld+json"> Product block  -> name, image[], description
  2. og:/meta tags                                              -> title, image, description
  3. DOM fallback (last resort)                                 -> heuristic selectors
The first strategy that yields a title wins; images/description backfill from any.

Auth: optional storage_state (same pattern as the Flipkart Autorunner). Public
PDPs usually load without login, but a warmed session reduces anti-bot friction.
Generate it once:  python flipkart_pdp_scraper.py --login
Then scrape:       python flipkart_pdp_scraper.py --worklist flipkart_worklist.csv

Politeness / robustness: low concurrency, jittered delays, per-FSN retry,
checkpointing (skips FSNs already in the output), and a failures report so a run
over ~640 pages is resumable and doesn't hammer the site.

NETWORK: the live scrape hits flipkart.com, unreachable from this sandbox. Run in
your environment. Use --selftest to validate the parser against a bundled fixture.
"""

import argparse
import asyncio
import csv
import json
import os
import random
import re
import sys

STORAGE_STATE = "flipkart_state.json"
OUT_DEFAULT = "observed_flipkart_v2.csv"
FAIL_DEFAULT = "observed_flipkart_v2_failures.csv"

OUT_COLS = [
    "nh_sku", "fsn", "pdp_availability", "obs_title", "obs_image_count",
    "obs_image_urls",
    "obs_description_len", "obs_description_text", "obs_price",
    "extraction_source", "obs_max_shelf_life", "obs_specifications", "scraped_at",
]

# ---- tunables ----
CONCURRENCY = 3
MIN_DELAY, MAX_DELAY = 1.5, 3.5     # seconds between page loads, per worker
NAV_TIMEOUT_MS = 30000
MAX_RETRIES = 2


# ===========================================================================
# Parsing — pure functions, unit-testable without a browser
# ===========================================================================
def _clean(text):
    return " ".join((text or "").split())


def parse_jsonld(html):
    """Return dict with title/images/description/price from a Product JSON-LD, or {}."""
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
        # some pages nest under @graph
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
    """og:/meta fallback."""
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
        "images": [img] if img else [],   # meta carries only 1 image; count unreliable
        "description": desc,
        "price": meta("product:price:amount"),
        "source": "meta",
    }


def _is_spec_row(node):
    """True if `node` is one row of Flipkart's structured Specifications grid:
    a field name (label_0, plain string) paired with a field value (label_1,
    list-of-string) -- e.g. {"label_0": "Maximum Shelf Life", "label_1": ["9 Months"]}
    once unwrapped. Flipkart wraps unrelated widgets (breadcrumbs, ratings,
    seller cards, nav actions) in the same label_0/label_1 naming convention,
    so this also rejects anything carrying nav/tracking/rating baggage that a
    real spec row never has.
    """
    if not isinstance(node, dict) or "label_0" not in node or "label_1" not in node:
        return False
    if any(k in node for k in (
        "action", "tracking", "trackerData_0", "ratingData_0", "icon_0",
        "row_0", "row_1", "row_2", "row_3",
    )):
        return False
    try:
        name = node["label_0"]["value"]["text"]
        val = node["label_1"]["value"]["text"]
    except (KeyError, TypeError):
        return False
    if not isinstance(name, str) or not name.strip():
        return False
    if not re.match(r"[A-Za-z]", name.strip()):
        # rejects the handful of non-spec label_0/label_1 pairs that share this
        # shape elsewhere on the page: seller-score chips ("1.2L+", "86%") and
        # price-offer callouts ("₹147") -- real spec field names are always
        # English phrases, never a bare number/percentage/currency amount.
        return False
    if name.strip() in ("Note",):  # installation-info bottom-sheet microcopy
        return False
    if isinstance(val, list):
        val = " ".join(str(v) for v in val if v)
    return isinstance(val, str) and bool(val.strip())


def extract_specifications(html):
    """Flipkart embeds a structured Specifications grid (Brand, Ideal For,
    Maximum Shelf Life, Skin Type, ...) inside the inline
    `window.__INITIAL_STATE__` JSON blob on the public PDP -- no login
    required. It lives many levels deep under dynamically-named widget/grid
    keys (e.g. "rpd_grid_0", "gridData_0") that are not guaranteed stable
    across page builds, so instead of a fixed JSON path, this parses the
    whole blob once and recursively collects every dict shaped like a spec
    row (see _is_spec_row). Returns {} if the blob is missing/unparseable or
    no spec rows are found -- never raises.
    """
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*</script>",
                  html, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}

    specs = {}

    def walk(node):
        if isinstance(node, dict):
            if _is_spec_row(node):
                name = _clean(node["label_0"]["value"]["text"])
                val = node["label_1"]["value"]["text"]
                if isinstance(val, list):
                    val = " ".join(_clean(str(v)) for v in val if v)
                if name and name not in specs:  # first occurrence wins
                    specs[name] = _clean(val)
                return  # a spec row doesn't nest further spec rows worth walking
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return specs


_SHELF_LIFE_SPEC_KEYS = ("Maximum Shelf Life", "Shelf Life")


def extract_max_shelf_life(html):
    """Convenience wrapper: the one field qa_diff.py needs right now."""
    specs = extract_specifications(html)
    for key in _SHELF_LIFE_SPEC_KEYS:
        if key in specs:
            return specs[key]
    return ""


def detect_unavailable(html):
    """Heuristic: is this a dead/suppressed PDP rather than a live product?"""
    low = html.lower()
    signals = [
        "this product is currently unavailable",
        "sold out",
        "page not found",
        "we could not find that page",
    ]
    return any(s in low for s in signals)


def extract(html, specs_html=None):
    """Merge strategies into a single observed record.

    specs_html: optionally, a *different* HTML source to search for the
    Specifications grid than the one used for title/images/description.
    Defaults to `html` when omitted (this is what every existing caller and
    fixture does). This split exists because of a real, confirmed gap: a
    live Playwright scrape of `page.content()` (the DOM *after* the page's
    JS has run) came back with obs_max_shelf_life empty on every single row
    of a 639-row production run -- including the exact FSN this feature was
    built and validated against -- even though the Specifications data is
    unquestionably present in that PDP's raw server HTML (confirmed via
    "View Page Source", which is what FIXTURE_WITH_SPECS is built from).
    The likely cause: Flipkart's PDP JS hydrates from the inline
    window.__INITIAL_STATE__ blob and then strips or replaces that <script>
    tag from the live DOM as a memory-saving step -- a common SPA pattern --
    so by the time page.content() serializes the DOM, the blob is gone,
    while title/JSON-LD/meta (core SEO tags the framework has no reason to
    remove) survive hydration fine, which matches exactly what was observed
    (638/639 titles extracted correctly, 0/639 specs found). The fix in
    _scrape_one() is to pass the *raw* navigation response body (identical
    in kind to "View Page Source", captured before any JS runs) as
    specs_html, independent of whichever HTML is used for everything else.
    """
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
            "obs_max_shelf_life": "",
            "obs_specifications": "",
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
            "obs_max_shelf_life": "", "obs_specifications": "",
        }
    # prefer JSON-LD images (full set); backfill description/price from whichever has it
    images = j.get("images") or m.get("images") or []
    desc = j.get("description") or m.get("description") or ""
    price = j.get("price") or m.get("price") or ""
    # Structured Specifications grid (public page, no login) -- see
    # extract_specifications(). Independent of the JSON-LD/meta strategies
    # above, so it's populated even when title/images came from meta fallback.
    # Searched in specs_html (raw pre-hydration response, when the caller has
    # one) rather than `html`, per the docstring above.
    specs = extract_specifications(specs_html if specs_html is not None else html)
    max_shelf_life = ""
    for key in _SHELF_LIFE_SPEC_KEYS:
        if key in specs:
            max_shelf_life = specs[key]
            break
    return {
        "pdp_availability": "available",
        "obs_title": primary["title"],
        "obs_image_count": len(images),
        "obs_image_urls": "|".join(images),
        "obs_description_len": len(desc),
        "obs_description_text": desc,
        "obs_price": price,
        "extraction_source": primary["source"],
        "obs_max_shelf_life": max_shelf_life,
        "obs_specifications": " | ".join(f"{k}: {v}" for k, v in specs.items()),
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
        return {r["fsn"] for r in csv.DictReader(f) if r.get("fsn")}


async def _do_login():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto("https://www.flipkart.com/")
        print("Log in in the opened browser, then press Enter here...")
        await asyncio.get_event_loop().run_in_executor(None, input)
        await ctx.storage_state(path=STORAGE_STATE)
        await browser.close()
        print(f"Saved session -> {STORAGE_STATE}")


async def _scrape_one(context, row, out_writer, fail_writer, lock):
    import datetime
    fsn, nh, url = row["fsn"], row["nh_sku"], row["pdp_url"]
    for attempt in range(1, MAX_RETRIES + 1):
        page = await context.new_page()
        try:
            resp = await page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            # give JSON-LD / meta a moment; PDPs inject it early
            await page.wait_for_timeout(1500)
            html = await page.content()
            # Specifications data lives in a raw, inline window.__INITIAL_STATE__
            # blob that Flipkart's PDP appears to strip from the DOM once its
            # own JS has hydrated from it -- confirmed by a real 639-row run
            # where page.content() alone found it on 0 rows, including FSNs
            # this feature was built and validated against. `resp` is the raw
            # navigation response body captured before any JS ran -- the same
            # thing "View Page Source" shows -- so it still has the blob.
            # Falls back to `html` if the response body can't be read for any
            # reason, so a hiccup here never blocks the rest of the row.
            try:
                specs_html = await resp.text()
            except Exception:  # noqa: BLE001
                specs_html = html
            rec = extract(html, specs_html=specs_html)
            rec.update({
                "nh_sku": nh, "fsn": fsn,
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
                    fail_writer.writerow({"nh_sku": nh, "fsn": fsn, "url": url,
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
    todo = [r for r in work if r["fsn"] not in done]
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
    fail_writer = csv.DictWriter(fail_f, fieldnames=["nh_sku", "fsn", "url", "error"])
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
        if os.path.exists(STORAGE_STATE):
            ctx_kwargs["storage_state"] = STORAGE_STATE
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
# Self-test (no browser, no network)
# ===========================================================================
FIXTURE_LIVE = """
<html><head>
<meta property="og:title" content="BUPA Ubtan Face Wash 150g | NatHabit">
<script type="application/ld+json">
{"@type":"Product","name":"BUPA Ubtan Face Wash 150g",
 "image":["https://rukmini/a.jpg","https://rukmini/b.jpg","https://rukmini/c.jpg"],
 "description":"Brightening ubtan face wash with turmeric and saffron. Removes tan.",
 "offers":{"@type":"Offer","price":"299","priceCurrency":"INR"}}
</script></head><body>...</body></html>
"""

FIXTURE_META_ONLY = """
<html><head>
<meta property="og:title" content="Hibiscus Shampoo 300ml">
<meta property="og:image" content="https://rukmini/hib.jpg">
<meta name="description" content="Hibiscus and amla shampoo for hair fall control.">
</head><body></body></html>
"""

FIXTURE_DEAD = """
<html><head><title>Flipkart</title></head>
<body><div>This product is currently unavailable.</div></body></html>
"""

# Real fragment (not hand-typed): the actual 19-row Specifications grid from
# the live FSN FCWGUA62VWP9E37J PDP ("Nat Habit Ubtan ... Face Wash", the page
# that surfaced this whole gap -- it shows "Maximum Shelf Life: 9 Months" in
# its on-page Specifications panel but that field is invisible to the old
# JSON-LD/meta-only extractor), re-nested under the exact widget/grid key
# names Flipkart used on that build (rpd_tab_showcase_vertical_list_0 ->
# rpd_specifications_grid_layout_1 -> rpd_grid_0 -> gridData_0), trimmed down
# to just this one widget slot so the fixture stays readable.
FIXTURE_WITH_SPECS = """
<html><body><script id="is_script">window.__INITIAL_STATE__ = {"multiWidgetState": {"widgetsData": {"slots": [{"slotData": {"widget": {"widgetType": "ATLAS_WIDGET", "data": {"dlsData": {"rpd_tab_showcase_vertical_list_0": {"value": {"rpd_specifications_grid_layout_1": {"value": {"gridData_0": {"value": [{"value": {"rpd_grid_0": {"value": {"gridData_0": {"value": [{"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["Nat Habit"]}}, "label_1": {"value": {"text": ["Nat Habit"]}}, "label_0": {"value": {"text": "Brand"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["No"]}}, "label_1": {"value": {"text": ["No"]}}, "label_0": {"value": {"text": "Prescription Required"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["100 g"]}}, "label_1": {"value": {"text": ["100 g"]}}, "label_0": {"value": {"text": "Quantity"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["Cream"]}}, "label_1": {"value": {"text": ["Cream"]}}, "label_0": {"value": {"text": "Face Wash Type"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["Natural"]}}, "label_1": {"value": {"text": ["Natural"]}}, "label_0": {"value": {"text": "Ingredient Type"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["Ubtan Face Wash for Women & Men | Natural Face wash for Clear Glowing Skin"]}}, "label_1": {"value": {"text": ["Ubtan Face Wash for Women & Men | Natural Face wash for Clear Glowing Skin"]}}, "label_0": {"value": {"text": "Model Name"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["Men & Women"]}}, "label_1": {"value": {"text": ["Men & Women"]}}, "label_0": {"value": {"text": "Ideal For"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["9 Months"]}}, "label_1": {"value": {"text": ["9 Months"]}}, "label_0": {"value": {"text": "Maximum Shelf Life"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["Skin Brightening, Tan Removal, Spot Removal, Radiance & Glow, Uneven Skin Tone"]}}, "label_1": {"value": {"text": ["Skin Brightening, Tan Removal, Spot Removal, Radiance & Glow, Uneven Skin Tone"]}}, "label_0": {"value": {"text": "Applied For"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["All Skin Types, Combination Skin, Dry Skin, Normal Skin, Oily Skin, Sensitive Skin"]}}, "label_1": {"value": {"text": ["All Skin Types, Combination Skin, Dry Skin, Normal Skin, Oily Skin, Sensitive Skin"]}}, "label_0": {"value": {"text": "Skin Type"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["Tube"]}}, "label_1": {"value": {"text": ["Tube"]}}, "label_0": {"value": {"text": "Container Type"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["Facewash"]}}, "label_1": {"value": {"text": ["Facewash"]}}, "label_0": {"value": {"text": "Type"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["Ground Wild Turmeric, Besan, Rakht Chandan"]}}, "label_1": {"value": {"text": ["Ground Wild Turmeric, Besan, Rakht Chandan"]}}, "label_0": {"value": {"text": "Composition"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["All Day"]}}, "label_1": {"value": {"text": ["All Day"]}}, "label_0": {"value": {"text": "Usage"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["Scented"]}}, "label_1": {"value": {"text": ["Scented"]}}, "label_0": {"value": {"text": "Fragrance"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["Sulphate/Paraben Free, Cruelty Free"]}}, "label_1": {"value": {"text": ["Sulphate/Paraben Free, Cruelty Free"]}}, "label_0": {"value": {"text": "Manufacturing Process"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["100 g"]}}, "label_1": {"value": {"text": ["100 g"]}}, "label_0": {"value": {"text": "Net Quantity"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["1 Ubtan Face Wash"]}}, "label_1": {"value": {"text": ["1 Ubtan Face Wash"]}}, "label_0": {"value": {"text": "Sales Package"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["1"]}}, "label_1": {"value": {"text": ["1"]}}, "label_0": {"value": {"text": "Pack of"}}}}]}}}}}]}}}}}}}}}}]}}, "additionalInfo": {}};</script></body></html>
"""

# Synthetic on purpose (unlike the fixture above): same real field names/
# values, but deliberately renamed widget/grid keys ("some_other_widget_7",
# "yet_another_grid_layout_3", "rpd_grid_9", "gridData_2"/"gridData_5" instead
# of Flipkart's actual "rpd_tab_showcase_vertical_list_0" / "rpd_grid_0" /
# etc). Exists purely to prove extract_specifications() finds spec rows by
# their shape, not by hard-coding today's key names -- which matter because
# those dynamic IDs are exactly what the project's earlier analysis flagged
# as unlikely to stay stable across Flipkart page builds/categories.
FIXTURE_SPECS_DIFFERENT_KEYS = """
<html><body><script id="is_script">window.__INITIAL_STATE__ = {"multiWidgetState": {"widgetsData": {"slots": [{"slotData": {"widget": {"widgetType": "ATLAS_WIDGET", "data": {"dlsData": {"some_other_widget_7": {"value": {"yet_another_grid_layout_3": {"value": {"gridData_2": {"value": [{"value": {"rpd_grid_9": {"value": {"gridData_5": {"value": [{"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["Nat Habit"]}}, "label_1": {"value": {"text": ["Nat Habit"]}}, "label_0": {"value": {"text": "Brand"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["9 Months"]}}, "label_1": {"value": {"text": ["9 Months"]}}, "label_0": {"value": {"text": "Maximum Shelf Life"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["All Skin Types, Combination Skin, Dry Skin, Normal Skin, Oily Skin, Sensitive Skin"]}}, "label_1": {"value": {"text": ["All Skin Types, Combination Skin, Dry Skin, Normal Skin, Oily Skin, Sensitive Skin"]}}, "label_0": {"value": {"text": "Skin Type"}}}}]}}}}}]}}}}}}}}}}]}}};</script></body></html>
"""


# Real title/JSON-LD fixture (FIXTURE_LIVE's shape) combined with a trimmed
# real specs blob, to prove extract() wires the two independent strategies
# together the way a real PDP actually looks (JSON-LD for title/images/desc
# *and* __INITIAL_STATE__ for specs, both present on the same page).
FIXTURE_LIVE_WITH_SPECS = """
<html><head><script type="application/ld+json">{"@type":"Product","name":"Nat Habit Ubtan Face Wash","image":["https://rukmini/ubtan1.jpg"],"description":"Ubtan face wash for glowing skin.","offers":{"@type":"Offer","price":"147"}}</script></head><body><script id="is_script">window.__INITIAL_STATE__ = {"multiWidgetState": {"widgetsData": {"slots": [{"slotData": {"widget": {"data": {"dlsData": {"rpd_tab_showcase_vertical_list_0": {"value": {"rpd_specifications_grid_layout_1": {"value": {"gridData_0": {"value": [{"value": {"rpd_grid_0": {"value": {"gridData_0": {"value": [{"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["Nat Habit"]}}, "label_1": {"value": {"text": ["Nat Habit"]}}, "label_0": {"value": {"text": "Brand"}}}}, {"value": {"box_1": {}, "box_0": {}, "label_2": {"value": {"text": ["9 Months"]}}, "label_1": {"value": {"text": ["9 Months"]}}, "label_0": {"value": {"text": "Maximum Shelf Life"}}}}]}}}}}]}}}}}}}}}}]}}};</script></body></html>
"""


def selftest():
    ok = True

    live = extract(FIXTURE_LIVE)
    assert live["pdp_availability"] == "available", live
    assert live["obs_title"] == "BUPA Ubtan Face Wash 150g", live
    assert live["obs_image_count"] == 3, live
    assert live["obs_image_urls"] == "https://rukmini/a.jpg|https://rukmini/b.jpg|https://rukmini/c.jpg", live
    assert live["extraction_source"] == "json-ld", live
    assert live["obs_description_len"] > 0, live
    print("PASS  live JSON-LD:", live["obs_title"], "| imgs", live["obs_image_count"],
          "| src", live["extraction_source"])

    meta = extract(FIXTURE_META_ONLY)
    assert meta["pdp_availability"] == "available", meta
    assert meta["obs_title"] == "Hibiscus Shampoo 300ml", meta
    assert meta["extraction_source"] == "meta", meta
    assert meta["obs_image_count"] == 1, meta
    assert meta["obs_image_urls"] == "https://rukmini/hib.jpg", meta
    print("PASS  meta fallback:", meta["obs_title"], "| src", meta["extraction_source"])

    dead = extract(FIXTURE_DEAD)
    assert dead["pdp_availability"] == "unavailable", dead
    print("PASS  dead PDP detected as:", dead["pdp_availability"])

    # A page with no __INITIAL_STATE__ blob at all (e.g. JSON-LD/meta-only
    # fixtures above) must degrade gracefully, not crash.
    assert extract_specifications(FIXTURE_LIVE) == {}, extract_specifications(FIXTURE_LIVE)
    assert live["obs_max_shelf_life"] == "", live
    assert live["obs_specifications"] == "", live
    print("PASS  no __INITIAL_STATE__ blob -> specs extraction degrades to {} (no crash)")

    # Malformed/truncated __INITIAL_STATE__ (e.g. a page load caught mid-stream)
    # must also degrade gracefully rather than raising.
    broken_json = '<html><script>window.__INITIAL_STATE__ = {"a": [1, 2,</script></html>'
    assert extract_specifications(broken_json) == {}, "malformed JSON blob should yield {}"
    print("PASS  malformed __INITIAL_STATE__ JSON -> {} (no crash)")

    specs_live = extract(FIXTURE_WITH_SPECS)
    assert specs_live["pdp_availability"] == "no_content", specs_live
    # (this fixture has no JSON-LD/meta title -- it only carries the
    # __INITIAL_STATE__ blob -- so extract() reports no_content for the
    # title/image side while the specs helpers below still find the grid;
    # on a real PDP both JSON-LD and __INITIAL_STATE__ are present together,
    # as proven against the live page below.)
    specs = extract_specifications(FIXTURE_WITH_SPECS)
    assert len(specs) == 19, specs
    assert specs["Maximum Shelf Life"] == "9 Months", specs
    assert specs["Brand"] == "Nat Habit", specs
    assert specs["Model Name"] == ("Ubtan Face Wash for Women & Men | "
                                    "Natural Face wash for Clear Glowing Skin"), specs
    assert extract_max_shelf_life(FIXTURE_WITH_SPECS) == "9 Months", specs
    print(f"PASS  real Specifications grid (FSN FCWGUA62VWP9E37J): {len(specs)} fields, "
          f"Maximum Shelf Life = {specs['Maximum Shelf Life']!r}")

    specs_diffkeys = extract_specifications(FIXTURE_SPECS_DIFFERENT_KEYS)
    assert specs_diffkeys.get("Maximum Shelf Life") == "9 Months", specs_diffkeys
    assert specs_diffkeys.get("Brand") == "Nat Habit", specs_diffkeys
    assert len(specs_diffkeys) == 3, specs_diffkeys
    print("PASS  spec extraction survives renamed dynamic widget/grid keys "
          "(rpd_grid_0 -> rpd_grid_9, etc.) -- found by shape, not by key name")

    combined = extract(FIXTURE_LIVE_WITH_SPECS)
    assert combined["pdp_availability"] == "available", combined
    assert combined["obs_title"] == "Nat Habit Ubtan Face Wash", combined
    assert combined["extraction_source"] == "json-ld", combined
    assert combined["obs_max_shelf_life"] == "9 Months", combined
    assert combined["obs_specifications"] == "Brand: Nat Habit | Maximum Shelf Life: 9 Months", combined
    print("PASS  real PDP shape (JSON-LD title/images + __INITIAL_STATE__ specs, "
          "both present) -- extract() wires both together:", combined["obs_title"],
          "| shelf life", combined["obs_max_shelf_life"])

    # This is the actual production fix: title/images come from `html`
    # (page.content(), post-hydration) while specs come from a *separate*
    # `specs_html` source (the raw pre-hydration response body) -- proving
    # extract() correctly keeps the two independent, since in production
    # Flipkart strips the specs blob from the post-hydration DOM but not
    # the JSON-LD/meta tags.
    split = extract(FIXTURE_LIVE, specs_html=FIXTURE_WITH_SPECS)
    assert split["obs_title"] == "BUPA Ubtan Face Wash 150g", split  # from `html`
    assert split["extraction_source"] == "json-ld", split
    assert split["obs_max_shelf_life"] == "9 Months", split  # from `specs_html`, not `html`
    # (not counting " | " occurrences to size this -- one real field value,
    # Model Name, legitimately contains a literal "|" itself)
    assert split["obs_specifications"].startswith("Brand: Nat Habit | "), split
    assert "Maximum Shelf Life: 9 Months" in split["obs_specifications"], split
    print("PASS  title/images and specs correctly sourced from two different "
          "HTML strings when specs_html is given separately (the real fix: "
          "post-hydration page.content() for title, raw pre-JS response.text() "
          "for specs)")

    print("\nAll parser self-tests passed." if ok else "FAILURES")


# ===========================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="open browser to save a session")
    ap.add_argument("--selftest", action="store_true", help="validate parser on fixtures")
    ap.add_argument("--worklist", default="flipkart_worklist.csv")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--failures", default=FAIL_DEFAULT)
    ap.add_argument("--limit", type=int, help="scrape only the first N (smoke test)")
    a = ap.parse_args()

    if a.selftest:
        selftest()
    elif a.login:
        asyncio.run(_do_login())
    else:
        asyncio.run(run(a.worklist, a.out, a.failures, a.limit))
