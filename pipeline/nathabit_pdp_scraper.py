#!/usr/bin/env python3
"""
nathabit.in (D2C / Shopify-backed, Next.js storefront) PDP scraper -- the "A1"
reference side of the Flipkart Listing-QA diff.

Why this needs its own parser (can't reuse flipkart_pdp_scraper.py's approach):
  On nathabit.in the JSON-LD Product block is INCOMPLETE -- it only carries a
  short marketing tagline as "description", a single hero image, and per-variant
  Shopify SKUs. The fields we actually need to diff against Flipkart --
  the real long description, the full ingredient list, and shelf-life/expiry --
  only exist in the *rendered* DOM, in specific labeled sections. Images are
  the same story: JSON-LD has 1 image, the real gallery (~10 photos) lives in
  the on-page carousel.

  This was confirmed against a real, complete PDP HTML dump (tikta-ubtan-facewash)
  provided by the requester -- every extraction landmark below (the "#reviews"
  anchor, the "Full Ingredient List" label, the "Expiry" label, and the
  "#productImageCarousel" id) is copied verbatim from that real page, not
  guessed. See selftest() -- its fixtures are genuine excerpts of that page,
  not hand-written approximations.

Extraction strategy per field (each has a documented fallback):
  title        -> og:title meta, else <title>
  description  -> DOM: sibling block right after the "#reviews" rating link
                  (the real long copy) -> falls back to JSON-LD description/
                  slogan (short tagline) if the DOM landmark isn't found, so a
                  markup change degrades the reading rather than losing it
  ingredients  -> DOM: text block that immediately follows a "Full Ingredient
                  List" label -> "" (not "not found") if absent -- some SKUs
                  (e.g. hard goods like combs) may genuinely have none
  shelf_life   -> DOM: value block next to an "Expiry" label -> ""
  images       -> DOM: real <img> srcs inside #productImageCarousel, filtered
                  to photo extensions (drops the share/UI icon) -> falls back
                  to the single JSON-LD image if the carousel isn't found
  variant_skus -> JSON-LD hasVariant[].sku -- NOTE: on this storefront this
                  turns out to be Shopify's internal numeric variant ID (its
                  own CMS payload elsewhere on the page labels the identical
                  value "shopifyVariantId"), not a human-readable SKU. It is
                  NOT usable as a join key back to nh_sku. Captured anyway as
                  reference/debug info. The nh_sku<->URL join is instead a
                  direct mapping you supply (see nathabit_worklist.csv below).

This is plain HTML (server-rendered by Next.js), NOT a JS-heavy SPA -- the
sample page's `view-source` already contains every field above. So this
scraper uses plain HTTP (httpx) rather than a browser: faster, lighter, no
Playwright dependency for this side. If a future page turns out to be
gated behind client-side rendering or a bot-check, use --render to fall back
to a headless-browser fetch for just that page (see fetch_html()).

NETWORK: hits nathabit.in, unreachable from a cloud sandbox -- run this on
your machine, same as the Flipkart scraper. Use --selftest to validate the
parser against the bundled real-page fixtures (no network needed).

Input: nathabit_worklist.csv with columns  nh_sku,url  -- your direct mapping
of each nh_sku to its nathabit.in PDP URL. (Auto-discovering this mapping
from the storefront itself doesn't work here -- see the variant_skus note
above -- so a supplied mapping is the primary path, not a fallback.)

Usage:
    python nathabit_pdp_scraper.py --selftest
    python nathabit_pdp_scraper.py --limit 10          # smoke test first, same pattern as the Flipkart side
    python nathabit_pdp_scraper.py                     # full run: scrapes nathabit_worklist.csv -> nathabit_reference.csv

Optional (only if you ever need to independently enumerate every nathabit.in
product URL, e.g. to sanity-check your mapping's coverage -- untested against
this specific headless storefront, may come back empty):
    python nathabit_pdp_scraper.py --discover                  # sitemap.xml, falls back to /products.json -> nathabit_products.csv
    python nathabit_pdp_scraper.py --discover --render         # last resort: harvest links from rendered collection pages
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET

PRODUCTS_CSV = "nathabit_worklist.csv"   # your nh_sku,url mapping -- see load_products()
OUT_DEFAULT = "nathabit_reference.csv"
FAIL_DEFAULT = "nathabit_reference_failures.csv"
BASE = "https://nathabit.in"

OUT_COLS = [
    "nh_sku", "handle", "url", "variant_skus", "title", "description", "ingredients",
    "shelf_life", "image_urls", "image_count", "extraction_flags", "scraped_at",
]

MIN_DELAY, MAX_DELAY = 1.0, 2.5
MAX_RETRIES = 2
PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".webp")


# ===========================================================================
# Parsing -- pure functions, unit-testable without a browser (see selftest)
# ===========================================================================
def _clean(text):
    return " ".join((text or "").split())


def parse_jsonld_product(html):
    """Return the JSON-LD Product block (not Organization/other @types), or {}."""
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    )
    for raw in blocks:
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            variants = data.get("hasVariant", [])
            skus = [v.get("sku") for v in variants if isinstance(v, dict) and v.get("sku")]
            if not skus and data.get("sku"):  # single-variant products may skip hasVariant
                skus = [data["sku"]]
            return {
                "name": _clean(data.get("name", "")),
                "description": _clean(data.get("description", "")),
                "slogan": _clean(data.get("slogan", "")),
                "image": data.get("image", ""),
                "variant_skus": skus,
            }
    return {}


def parse_meta(html):
    def meta(prop):
        m = re.search(
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\'](.*?)["\']',
            html, re.IGNORECASE,
        )
        return _clean(m.group(1)) if m else ""

    title = meta("og:title")
    if not title:
        m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = _clean(m.group(1)) if m else ""
    return {"title": title, "description": meta("og:description"), "image": meta("og:image")}


def _get_soup(html):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")


def parse_description_dom(soup):
    """Real long description = the text block right after the '#reviews' rating link.
    That anchor is a stable functional landmark (drives the on-page review-jump
    link) independent of the Tailwind class churn around it."""
    a = soup.find("a", href="#reviews")
    if not a:
        return ""
    # rating row -> its parent (title+rating block) -> next sibling holds the copy
    block = a.parent
    for _ in range(3):
        if block is None:
            return ""
        sib = block.find_next_sibling()
        if sib is not None:
            text = _clean(sib.get_text(" ", strip=True))
            if len(text) > 30:
                return text
        block = block.parent
    return ""


def parse_ingredients_dom(soup):
    """Text immediately following a 'Full Ingredient List' label. Some product
    types (non-topicals) may legitimately have none -- absence isn't an error."""
    label = None
    for p in soup.find_all(["p", "h2", "h3", "span"]):
        if p.get_text(strip=True).lower() == "full ingredient list":
            label = p
            break
    if not label:
        return ""
    node = label
    for _ in range(6):
        node = node.parent
        if node is None:
            return ""
        sib = node.find_next_sibling()
        if sib is not None:
            text = _clean(sib.get_text(" ", strip=True))
            if len(text) > 15:
                return text
    return ""


def parse_shelf_life_dom(soup):
    """Value next to an 'Expiry' label (lives in a 'Storage & Expiry' block)."""
    label = None
    for p in soup.find_all(["p", "span"]):
        if p.get_text(strip=True).lower() == "expiry":
            label = p
            break
    if not label:
        return ""
    node = label
    for _ in range(4):
        node = node.parent
        if node is None:
            return ""
        sib = node.find_next_sibling()
        if sib is not None:
            text = _clean(sib.get_text(" ", strip=True))
            if text:
                return text
    return ""


def parse_gallery_images_dom(soup):
    """Real photo URLs from the product image carousel, icons/UI chrome dropped."""
    carousel = soup.find(id="#productImageCarousel") or soup.find(
        lambda t: t.has_attr("id") and "productImageCarousel" in t["id"]
    )
    if not carousel:
        return []
    urls = []
    for img in carousel.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src:
            continue
        base = src.split("?")[0]
        if base.lower().endswith(PHOTO_EXTS):
            if base not in urls:
                urls.append(base)
    return urls


def extract(html):
    """Merge JSON-LD (join key + fallback text) with DOM (the real content)."""
    soup = _get_soup(html)
    j = parse_jsonld_product(html)
    m = parse_meta(html)
    flags = []

    title = m.get("title") or j.get("name") or ""
    if not title:
        flags.append("no_title")

    description = parse_description_dom(soup)
    if not description:
        description = j.get("description") or j.get("slogan") or m.get("description") or ""
        flags.append("description_from_fallback")

    ingredients = parse_ingredients_dom(soup)
    if not ingredients:
        flags.append("no_ingredients_found")

    shelf_life = parse_shelf_life_dom(soup)
    if not shelf_life:
        flags.append("no_shelf_life_found")

    images = parse_gallery_images_dom(soup)
    if not images:
        img = j.get("image") or m.get("image") or ""
        images = [img] if img else []
        flags.append("images_from_fallback")

    return {
        "variant_skus": "|".join(j.get("variant_skus", [])),
        "title": title,
        "description": description,
        "ingredients": ingredients,
        "shelf_life": shelf_life,
        "image_urls": "|".join(images),
        "image_count": len(images),
        "extraction_flags": ";".join(flags),
    }


# ===========================================================================
# Discovery -- find every product URL on the storefront
# ===========================================================================
def discover_via_sitemap(fetch):
    """Standard Shopify/most storefronts expose /sitemap.xml -> child sitemaps
    -> product sitemaps listing every /products/<handle> URL."""
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root_xml = fetch(f"{BASE}/sitemap.xml")
    if not root_xml:
        return []
    try:
        root = ET.fromstring(root_xml)
    except ET.ParseError:
        return []
    child_sitemaps = [loc.text for loc in root.findall(".//sm:loc", ns)]
    product_sitemaps = [u for u in child_sitemaps if "product" in (u or "").lower()]
    if not product_sitemaps and child_sitemaps:
        # not an index -- root itself might already be a flat sitemap of URLs
        product_sitemaps = [f"{BASE}/sitemap.xml"]

    handles = set()
    for sm_url in product_sitemaps:
        xml_text = fetch(sm_url)
        if not xml_text:
            continue
        try:
            sm_root = ET.fromstring(xml_text)
        except ET.ParseError:
            continue
        for loc in sm_root.findall(".//sm:loc", ns):
            u = loc.text or ""
            m = re.search(r"/products/([^/?#]+)", u)
            if m:
                handles.add(m.group(1))
    return sorted(handles)


def discover_via_products_json(fetch):
    """Fallback: Shopify's classic /products.json endpoint (may not be exposed
    on a headless storefront -- worth trying, cheap to rule out)."""
    handles = set()
    page = 1
    while True:
        text = fetch(f"{BASE}/products.json?limit=250&page={page}")
        if not text:
            break
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            break
        products = data.get("products", [])
        if not products:
            break
        for p in products:
            if p.get("handle"):
                handles.add(p["handle"])
        page += 1
        if page > 40:  # safety valve, ~10k products
            break
    return sorted(handles)


def discover(fetch):
    handles = discover_via_sitemap(fetch)
    method = "sitemap"
    if not handles:
        print("  sitemap discovery returned nothing, trying /products.json ...")
        handles = discover_via_products_json(fetch)
        method = "products.json"
    if not handles:
        sys.exit(
            "Could not discover any product URLs via sitemap.xml or products.json.\n"
            "This can happen if the storefront is headless (custom Next.js frontend,\n"
            "no classic Shopify sitemap/JSON exposed) or is behind a bot-check.\n"
            "Try --render (renders the homepage/collections with a browser and\n"
            "harvests /products/ links from the rendered DOM instead), or send me\n"
            "a saved HTML dump of a collection page and I'll add a DOM-based\n"
            "discovery path the same way we did for the PDP fields."
        )
    print(f"  discovered {len(handles)} product handles via {method}")
    with open(PRODUCTS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["handle", "url"])
        for h in handles:
            w.writerow([h, f"{BASE}/products/{h}"])
    print(f"  -> {PRODUCTS_CSV}")


def discover_via_render(render_fetch):
    """Last-resort discovery: render a few index pages and harvest /products/
    links from the live DOM. Only used with --discover --render."""
    seeds = [BASE, f"{BASE}/collections/all", f"{BASE}/collections/all?page=2",
             f"{BASE}/collections/all?page=3"]
    handles = set()
    for url in seeds:
        html = render_fetch(url)
        if not html:
            continue
        for m in re.finditer(r'/products/([a-zA-Z0-9\-]+)', html):
            handles.add(m.group(1))
    return sorted(handles)


# ===========================================================================
# Fetching (plain HTTP by default; --render uses a headless browser instead)
# ===========================================================================
def make_http_fetch():
    import httpx
    client = httpx.Client(
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"},
        timeout=20.0, follow_redirects=True,
    )

    def fetch(url):
        try:
            r = client.get(url)
            if r.status_code == 200:
                return r.text
            fetch.last_error = f"http_{r.status_code}"
            return None
        except httpx.HTTPError as e:
            fetch.last_error = f"{type(e).__name__}: {e}"[:200]
            return None
    fetch.last_error = ""
    return fetch


def make_render_fetch():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context()

    def fetch(url):
        page = ctx.new_page()
        try:
            page.goto(url, timeout=30000, wait_until="networkidle")
            return page.content()
        except Exception:
            return None
        finally:
            page.close()
    return fetch


# ===========================================================================
# Scrape orchestration
# ===========================================================================
def _handle_from_url(url):
    m = re.search(r"/products/([^/?#]+)", url or "")
    return m.group(1) if m else ""


def load_products(path):
    """Expects a CSV with at least a 'url' column. An 'nh_sku' column, if
    present, is carried straight through to the output -- this is the direct,
    authoritative mapping (no guessing at Shopify variant SKUs needed, since
    the storefront doesn't expose a usable one -- see the module docstring).
    'handle' is derived from the URL if not supplied.

    Tolerant of stray whitespace in headers/values (e.g. a hand-built CSV
    from `echo` on Windows can leave a trailing space on the last column
    name or a value) -- everything is stripped before use."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = [h.strip() for h in next(reader, [])]
        rows = [dict(zip(header, [v.strip() for v in row])) for row in reader if row]
    if rows and "url" not in header:
        sys.exit(f"{path} needs a 'url' column (nh_sku column optional but recommended). "
                  f"Found columns: {header}")
    return rows


def _done_key(r):
    """nh_sku is the real output granularity (one row per nh_sku, even when
    several nh_skus share a D2C URL) -- fall back to url only for rows that
    genuinely have no nh_sku."""
    return r.get("nh_sku") or r.get("url")


def load_done(out_path):
    if not os.path.exists(out_path):
        return set()
    with open(out_path, newline="", encoding="utf-8") as f:
        return {_done_key(r) for r in csv.DictReader(f) if _done_key(r)}


def run(products_path, out_path, fail_path, fetch, limit=None):
    import datetime
    if not os.path.exists(products_path):
        sys.exit(
            f"{products_path} not found.\n"
            "This needs a CSV with your nh_sku -> nathabit.in URL mapping, columns:\n"
            "    nh_sku,url\n"
            "(handle is derived from the URL automatically; variant_skus in the\n"
            "output is bonus info, not needed for the join since you're supplying\n"
            "the mapping directly). --discover can attempt to auto-build a "
            "handle-only list instead, but a direct mapping is more reliable here."
        )
    products = load_products(products_path)
    no_url = [p for p in products if not (p.get("url") or "").strip()]
    products = [p for p in products if (p.get("url") or "").strip()]
    done = load_done(out_path)
    todo = [p for p in products if _done_key(p) not in done]
    if limit:
        todo = todo[:limit]
    print(f"Products {len(products)} (+{len(no_url)} skipped, no url yet) "
          f"| already done {len(done)} | to scrape {len(todo)}")
    if not todo:
        return

    out_is_new = not os.path.exists(out_path)
    with open(out_path, "a", newline="", encoding="utf-8") as outf, \
         open(fail_path, "a", newline="", encoding="utf-8") as failf:
        out_w = csv.DictWriter(outf, fieldnames=OUT_COLS)
        fail_w = csv.DictWriter(failf, fieldnames=["nh_sku", "url", "error"])
        if out_is_new:
            out_w.writeheader()
        if os.path.getsize(fail_path) == 0:
            fail_w.writeheader()

        for i, row in enumerate(todo, 1):
            nh_sku, url = row.get("nh_sku", ""), row["url"]
            handle = row.get("handle") or _handle_from_url(url)
            for attempt in range(1, MAX_RETRIES + 1):
                html = fetch(url)
                if html:
                    rec = extract(html)
                    rec.update({
                        "nh_sku": nh_sku, "handle": handle, "url": url,
                        "scraped_at": datetime.datetime.now().isoformat(timespec="seconds"),
                    })
                    out_w.writerow(rec)
                    outf.flush()
                    break
                if attempt == MAX_RETRIES:
                    err = getattr(fetch, "last_error", "") or "fetch_failed"
                    fail_w.writerow({"nh_sku": nh_sku, "url": url, "error": err})
                    failf.flush()
                else:
                    time.sleep(2 * attempt)
            if i % 25 == 0:
                print(f"  ...{i}/{len(todo)} scraped", file=sys.stderr)
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    # Drop stale failure entries for nh_skus that succeeded on this (or an
    # earlier) retry -- otherwise the failures file only grows and the count
    # below would over-report.
    n_fail = 0
    if os.path.exists(fail_path):
        succeeded = load_done(out_path)
        with open(fail_path, newline="", encoding="utf-8") as f:
            # keep only the LAST (most recent) error per SKU -- a SKU that
            # failed in an earlier run and failed again this run should
            # appear once, with its latest error message, not once per run
            latest_by_key = {}
            for r in csv.DictReader(f):
                if _done_key(r) not in succeeded:
                    latest_by_key[_done_key(r)] = r
            still_failing = list(latest_by_key.values())
        with open(fail_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["nh_sku", "url", "error"])
            w.writeheader()
            w.writerows(still_failing)
        n_fail = len(still_failing)
    print(f"Done. {out_path} ready. Failures: {n_fail} (see {fail_path}). "
          f"Re-run the same command to retry just those (it skips rows already done).")


# ===========================================================================
# Selftest -- fixtures are verbatim excerpts of a real nathabit.in PDP
# (tikta-ubtan-facewash), not hand-written approximations. This means the
# extraction landmarks (the #reviews anchor, "Full Ingredient List" label,
# "Expiry" label, #productImageCarousel id) are proven against real markup,
# the same standard used for the Flipkart-side selftest.
# ===========================================================================
def _build_fixture():
    from _fixture_data import (
        LDJSON, TITLE_TAG, OG_TITLE, DESC_FRAGMENT,
        INGREDIENTS_FRAGMENT, EXPIRY_FRAGMENT, CAROUSEL_FRAGMENT,
    )
    return f"""<!DOCTYPE html><html><head>{TITLE_TAG}{OG_TITLE}{LDJSON}</head>
<body>{DESC_FRAGMENT}{INGREDIENTS_FRAGMENT}{EXPIRY_FRAGMENT}{CAROUSEL_FRAGMENT}</body></html>"""


def selftest():
    html = _build_fixture()
    rec = extract(html)
    checks = []

    checks.append(("title", rec["title"] == "Brightening Ubtan Tikta Face Wash"))
    checks.append(("variant_skus captured (reference only, not a join key -- see docstring)",
                    rec["variant_skus"] == "48453018255666|48453018288434"))
    checks.append(("description is the real DOM copy, not the JSON-LD tagline",
                    rec["description"].startswith("Experience the magic of true ubtan")))
    checks.append(("ingredients extracted",
                    rec["ingredients"].startswith("wild kasturi, rakht chandan")))
    checks.append(("shelf_life extracted",
                    rec["shelf_life"] == "Use within 9 months of Mfg Date"))
    checks.append(("gallery images found, icon excluded",
                    rec["image_count"] == 3 and "share-icon" not in rec["image_urls"]))
    checks.append(("no fallback flags fired on a clean page",
                    rec["extraction_flags"] == ""))

    ok = True
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    # degraded-page case: strip every DOM landmark, only JSON-LD + meta survive
    from _fixture_data import LDJSON, TITLE_TAG, OG_TITLE
    degraded_html = f"<!DOCTYPE html><html><head>{TITLE_TAG}{OG_TITLE}{LDJSON}</head><body></body></html>"
    drec = extract(degraded_html)
    degraded_ok = (
        drec["description"] == "Brightening Ubtan Tikta Face Wash"
        and drec["ingredients"] == "" and drec["shelf_life"] == ""
        and "description_from_fallback" in drec["extraction_flags"]
        and "no_ingredients_found" in drec["extraction_flags"]
    )
    print(f"  [{'PASS' if degraded_ok else 'FAIL'}] degrades to JSON-LD fallback without crashing when DOM landmarks are missing")
    ok = ok and degraded_ok

    print("SELFTEST", "PASSED" if ok else "FAILED")
    if not ok:
        sys.exit(1)


# ===========================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--discover", action="store_true",
                     help="find product URLs -> nathabit_products.csv, then exit")
    ap.add_argument("--render", action="store_true",
                     help="use a headless browser instead of plain HTTP "
                          "(for discovery, or if plain-HTTP scraping starts "
                          "coming back empty / bot-checked)")
    ap.add_argument("--products", default=PRODUCTS_CSV)
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--fail", default=FAIL_DEFAULT)
    ap.add_argument("--limit", type=int, default=None,
                     help="scrape only the first N (smoke test)")
    a = ap.parse_args()

    if a.selftest:
        selftest()
        sys.exit(0)

    if a.discover:
        if a.render:
            print("Discovering via rendered DOM (headless browser)...")
            handles = discover_via_render(make_render_fetch())
            if not handles:
                sys.exit("Rendered discovery found no /products/ links either -- "
                          "send me a saved HTML dump of nathabit.in/collections/all "
                          "and I'll build a targeted path.")
            with open(PRODUCTS_CSV, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["handle", "url"])
                for h in handles:
                    w.writerow([h, f"{BASE}/products/{h}"])
            print(f"  discovered {len(handles)} product handles via rendered DOM -> {PRODUCTS_CSV}")
        else:
            print("Discovering product URLs (sitemap.xml, falling back to products.json)...")
            discover(make_http_fetch())
        sys.exit(0)

    fetch = make_render_fetch() if a.render else make_http_fetch()
    run(a.products, a.out, a.fail, fetch, limit=a.limit)
