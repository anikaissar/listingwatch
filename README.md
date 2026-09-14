# Listing QA — KAM Dashboard

Weekly automated comparison of marketplace listings (Flipkart, Nykaa,
Myntra) against nathabit.in (the D2C site, treated as the source of truth),
surfaced as a live dashboard the KAM works from.

- **`docs/`** — the dashboard itself (GitHub Pages serves this folder).
- **`worker/`** — the Cloudflare Worker that saves Status changes made in
  the dashboard back into `docs/data.json`.
- **`pipeline/`** — the scraper, diff engine, and the weekly orchestrator
  script (`weekly_refresh.py`) that ties them together and pushes the
  refreshed data.

**Start here: [`SETUP.md`](./SETUP.md)** — one-time setup for all three
pieces, in order.

## Priority tiers

- **P1 — Shelf Life Mismatch** — the platform's stated shelf life doesn't
  match the D2C listing. Flipkart also has a flat "3 Months" default
  sub-case (one catalog-team escalation covers every SKU showing it, not a
  per-SKU fix). Nykaa never fires P1 -- confirmed (2026-09-11) that Nykaa's
  own shelf-life field is null on every product checked, so there's nothing
  on Nykaa's side to compare. Myntra does fire P1, compared only against
  its "Total Shelf Life in Months" field (the maximum-duration one, by
  Anika's explicit choice) -- confirmed (2026-09-14) that this field is
  genuinely populated on Myntra, unlike Nykaa.
- **P2 — Page Broken** — the platform's page is broken, delisted, or a
  generic placeholder (Flipkart's placeholder title; Nykaa's own
  404/isNotFound signal; Myntra's pdpData being completely absent).
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

(`pipeline/build_kam_review.py` emits this tier numbering directly, via
`classify()` for Flipkart, `classify_nykaa()` for Nykaa, and
`classify_myntra()` for Myntra -- all three return the same P1-P5 tier
numbering and labels, kept deliberately independent per platform rather than
one shared function, since each platform's real signals differ (see the P1/
P2 bullets above). An older 5-tier scheme existed briefly during development,
with a remap step in `docs/index.html` to translate it -- that remap was
removed on 2026-09-11 after it was found silently re-remapping already-correct
values (e.g. a real P2 "Page Broken" row was being shown under P1). The
scripts and the dashboard now agree on P1-P5 with no translation needed. P5
was added on 2026-09-14 to stop SKUs with no D2C reference match from being
silently dropped from the QA sweep entirely -- see `qa_diff.py` /
`nykaa_qa_diff.py` / `myntra_qa_diff.py`'s `no_d2c_match` column.)

## How data flows

```
Anika's Windows machine (weekly, scheduled)
  flipkart_pdp_scraper.py + nykaa_pdp_scraper.py + myntra_pdp_scraper.py
  + nathabit_pdp_scraper.py (the shared D2C reference, one scrape for all)
        -> qa_diff.py / nykaa_qa_diff.py / myntra_qa_diff.py
        -> weekly_refresh.py merges forward Status/Reminders Sent, per platform
        -> commits docs/data.json, git push
                 |
                 v
        GitHub Pages redeploys docs/ automatically
                 |
                 v
        Dashboard (docs/index.html) polls data.json, renders it with a tab per platform

Daksh/Anika, any time
  click a Status dropdown in the dashboard
        -> POST to the Cloudflare Worker (worker/)
        -> Worker commits the change to docs/data.json via the GitHub API
                 |
                 v
        GitHub Pages redeploys; other viewers see it within ~1 minute
```

Nykaa and Myntra both need a real, visible browser window to scrape
(`--headed` -- both sites block headless Chromium outright, confirmed
2026-09-11 and 2026-09-14 respectively), so `weekly_refresh.py` only works
in an interactive desktop session -- see its module docstring and SETUP.md.

Status and Reminders Sent live in git history from now on — every change,
whether from a weekly refresh or a manual click, is a commit, so there's a
full audit trail for free.
