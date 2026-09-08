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

(This is a renumbered 4-tier scheme. `pipeline/build_kam_review.py` still
emits the old 5-tier numbering (P1-P5, where P1 and P2 were both shelf-life
mismatches); the dashboard remaps old P1/P2 -> new P1, old P3 -> new P2, old
P4 -> new P3, old P5 -> new P4. Update the script to emit the new numbering
directly, then remove that remap from `docs/index.html`.)

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
