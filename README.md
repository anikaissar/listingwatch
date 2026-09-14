# Flipkart QA — KAM Dashboard

Weekly automated comparison of Flipkart marketplace listings against
nathabit.in (the D2C site, treated as the source of truth), surfaced as a
live dashboard the KAM works from.

- **`docs/`** — the dashboard itself (GitHub Pages serves this folder).
- **`worker/`** — the Cloudflare Worker that saves Status changes made in
  the dashboard back into `docs/data.json`.
- **`pipeline/`** — the scraper, diff engine, and the weekly orchestrator
  script (`weekly_refresh.py`) that ties them together and pushes the
  refreshed data.

**Start here: [`SETUP.md`](./SETUP.md)** — one-time setup for all three
pieces, in order.

## Priority tiers

- **P1 — Shelf Life Mismatch** — shelf life on Flipkart doesn't match the D2C
  listing, including cases where Flipkart states a flat "3 Months" default
  regardless of the actual product.
- **P2 — Page Broken** — Flipkart page is broken or a generic placeholder.
- **P3 — Title Mismatch** — title looks implausible for the SKU, worth a
  confirm.
- **P4 — Product Photo Mismatch** — product photo is a weak visual match to
  the D2C listing.
- **P5 — No D2C Reference Match** — this SKU has no row at all in the D2C
  reference file, so there's nothing to compare against on any other tier.
  Deliberately NOT labeled "discontinued" -- it could just as easily be a
  marketplace-only SKU never meant to be on nathabit.in, a D2C scrape that
  hasn't reached it yet, or a stale D2C URL. The action is neutral: check
  the SKU codes master for that SKU's live status.

(`pipeline/build_kam_review.py` emits this tier numbering directly. An
older 5-tier scheme existed briefly during development, with a remap step in
`docs/index.html` to translate it -- that remap was removed on 2026-09-11
after it was found silently re-remapping already-correct values (e.g. a real
P2 "Page Broken" row was being shown under P1). Both the script and the
dashboard now agree on P1-P5 as listed above with no translation needed. P5
was added on 2026-09-14 to stop SKUs with no D2C reference match from being
silently dropped from the QA sweep entirely -- see `qa_diff.py` /
`nykaa_qa_diff.py`'s `no_d2c_match` column.)

## How data flows

```
Anika's Windows machine (weekly, scheduled)
  flipkart_pdp_scraper.py + nathabit_pdp_scraper.py
        -> qa_diff.py
        -> weekly_refresh.py merges forward Status/Reminders Sent
        -> commits docs/data.json, git push
                 |
                 v
        GitHub Pages redeploys docs/ automatically
                 |
                 v
        Dashboard (docs/index.html) polls data.json, renders it

Daksh/Anika, any time
  click a Status dropdown in the dashboard
        -> POST to the Cloudflare Worker (worker/)
        -> Worker commits the change to docs/data.json via the GitHub API
                 |
                 v
        GitHub Pages redeploys; other viewers see it within ~1 minute
```

Status and Reminders Sent live in git history from now on — every change,
whether from a weekly refresh or a manual click, is a commit, so there's a
full audit trail for free.
