"""
One-off debug helper: saves the fully-rendered HTML of a single Nykaa
product page to disk, so we can inspect the real DOM for things like
shelf-life/expiry text that aren't showing up in our extraction yet.

Usage:
    python pipeline/dump_nykaa_html.py 12917343
    (saves to pipeline/nykaa_dump_12917343.html)

Always runs headed (like the real scraper needs to for Nykaa).
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright


async def main(product_id: str):
    url = f"https://www.nykaa.com/x/p/{product_id}?productId={product_id}"
    out_path = Path(__file__).parent / f"nykaa_dump_{product_id}.html"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=["--disable-http2"])
        context = await browser.new_context(
            locale="en-IN",
            viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
        )
        page = await context.new_page()
        print(f"Navigating to {url} ...")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # give the page a bit to hydrate / lazy-load tabs like "Product Details"
        await page.wait_for_timeout(4000)
        # try to click any "Product Details" / "more" expander if present, best-effort
        for text in ["Product Details", "View More", "Read More", "Know Your Product"]:
            try:
                loc = page.get_by_text(text, exact=False).first
                if await loc.count() > 0:
                    await loc.click(timeout=1500)
                    await page.wait_for_timeout(1000)
            except Exception:
                pass
        html = await page.content()
        out_path.write_text(html, encoding="utf-8")
        print(f"Saved {len(html)} chars -> {out_path}")
        await browser.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python pipeline/dump_nykaa_html.py <product_id>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
