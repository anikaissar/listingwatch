# Listing QA — KAM Dashboard

Weekly automated comparison of marketplace listings (Flipkart, Nykaa,
Myntra, Amazon) against nathabit.in (the D2C site, treated as the source of
truth), surfaced as a live dashboard the KAM works from.

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
  genuinely populated on Myntra, unlike Nykaa. Amazon also fires P1 --
  unlike Myntra's dedicated field, Amazon has no single structured
  shelf-life field, so it's parsed from free text under a "Storage:" label
  inside the page's "Important information" block (confirmed 2026-09-14
  against real ASIN B0BK1V96Z4 / SKU FC-KL-OK-040: D2C says "2 months",
  Amazon's Storage text says "30days" -- a genuine mismatch).
- **P2 — Page Broken** — the platform's page is broken, delisted, or a
  generic placeholder (Flipkart's placeholder title; Nykaa's own
  404/isNotFound signal; Myntra's pdpData being completely absent; Amazon's
  own "Page Not Found" static page, confirmed against a deliberately bogus
  ASIN). Amazon's separate soft anti-bot interstitial ("Click the button
  below to continue shopping") is a different thing entirely -- it's caught
  and retried automatically at scrape time and never reaches this far, so it
  can never be misread as a real "page broken" signal.
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
`classify()` for Flipkart, `classify_nykaa()` for Nykaa, `classify_myntra()`
for Myntra, and `classify_amazon()` for Amazon -- all four return the same
P1-P5 tier numbering and labels, kept deliberately independent per platform
rather than one shared function, since each platform's real signals differ
(see the P1/P2 bullets above). An older 5-tier scheme existed briefly during
development, with a remap step in `docs/index.html` to translate it -- that
remap was removed on 2026-09-11 after it was found silently re-remapping
already-correct values (e.g. a real P2 "Page Broken" row was being shown
under P1). The scripts and the dashboard now agree on P1-P5 with no
translation needed. P5 was added on 2026-09-14 to stop SKUs with no D2C
reference match from being silently dropped from the QA sweep entirely --
see `qa_diff.py` / `nykaa_qa_diff.py` / `myntra_qa_diff.py` /
`amazon_qa_diff.py`'s `no_d2c_match` column.)

## How data flows

```
Anika's Windows machine (weekly, scheduled)
  flipkart_pdp_scraper.py + nykaa_pdp_scraper.py + myntra_pdp_scraper.py
  + amazon_pdp_scraper.py
  + nathabit_pdp_scraper.py (the shared D2C reference, one scrape for all)
        -> qa_diff.py / nykaa_qa_diff.py / myntra_qa_diff.py / amazon_qa_diff.py
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
Amazon is different: it doesn't block headless Chromium at all, but has its
own soft anti-bot interstitial that shows up occasionally based on request
pacing rather than a hard per-ASIN/per-session ban -- `amazon_pdp_scraper.py`
runs headless by default, detects the interstitial, and retries automatically
at a deliberately low concurrency with longer delays between requests than
Nykaa/Myntra use (see the module docstring's "AMAZON'S BOT-CHECK" section).

Status and Reminders Sent live in git history from now on — every change,
whether from a weekly refresh or a manual click, is a commit, so there's a
full audit trail for free.

**Amazon rollout status (as of 2026-09-14):** the worklist
(`pipeline/amazon_worklist.csv`, 530 rows / 516 distinct SKUs), scraper,
diff engine, and classifier are all built and validated against real Amazon
pages, and `weekly_refresh.py` is fully wired up (`--skip-amazon`,
`--amazon-worklist`, `--amazon-min-rows`, `--amazon-max-drop-pct` all work
the same way their Nykaa/Myntra counterparts do). What hasn't happened yet
is a real full-catalog Amazon run -- until one has been done and checked
over, Amazon's dashboard tab still shows "Soon" and isn't in
`PLATFORMS_WITH_DATA` in `docs/index.html`. Flip that (and drop the "Soon"
span on the tab button) once a real run looks right, same as Nykaa and
Myntra before it.

## Reminder cycles

Each row's `reminders_sent` count used to bump by 1 every time
`weekly_refresh.py` ran and found the row still open -- which meant a SKU
could rack up several "reminders" within the same real week if the script
happened to run more than once that week (exactly what happened during
Myntra's rollout/testing: some rows hit 5 before Myntra had even gone live).
As of 2026-09-14, reminders are calendar-anchored instead: `CYCLE_ANCHOR =
2026-09-24` in `weekly_refresh.py` marks the start of cycle 1, the week
after is cycle 2, and so on, and each row now also carries
`last_reminder_cycle` so a second run inside the same week is a safe
no-op -- `reminders_sent` increments at most once per real week no matter
how many times the pipeline actually runs. Before 2026-09-24 nothing
increments at all. `docs/index.html` mirrors the same anchor/cycle math in
JS (`currentCycle()`) so the dashboard's "Total tracked" stat tile shows the
actual current cycle number ("reminder cycle 2 (since 2026-09-24)") instead
of a sum of every row's reminder count, which used to read like a number
tied to catalog size rather than elapsed weeks. Every platform's
`reminders_sent` was manually reset to 0 in `docs/data.json` on 2026-09-14
as part of this change, so cycle 1 starts genuinely clean.
