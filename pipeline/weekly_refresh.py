#!/usr/bin/env python3
"""Weekly refresh for the Listing QA KAM dashboard (Flipkart + Nykaa + Myntra + Amazon).

Run this on Anika's Windows machine (via Task Scheduler) once a week. It:

  1. Scrapes current Flipkart + Nykaa + Myntra + Amazon + nathabit.in
     listing data (reusing a browser session that was already logged in
     manually for Flipkart -- see "Login" below; Nykaa, Myntra, and Amazon
     need no login. Nykaa and Myntra also need a real, visible browser
     window -- see "NYKAA/MYNTRA NEED --headed" below; Amazon does NOT need
     --headed, but does need pacing -- see "AMAZON'S BOT-CHECK" below).
  2. Diffs each platform against the same nathabit.in D2C reference
     (qa_diff.py for Flipkart, nykaa_qa_diff.py for Nykaa, myntra_qa_diff.py
     for Myntra, amazon_qa_diff.py for Amazon) and classifies the results
     into priority tiers (classify()/classify_nykaa()/classify_myntra()/
     classify_amazon() from build_kam_review.py, imported here so all four
     never drift apart). Every diff now runs with --visual (see "IMAGE
     COMPARISON" below) so P4 (Product Photo Mismatch) actually has data to
     act on.
  3. Merges the fresh results, per platform, against whatever is CURRENTLY
     in the repo's docs/data.json -- same merge-forward rules as before: a
     brand new row starts at Open/0 reminders; a row still Open and still
     flagged gets +1 reminder; a row marked "Not an Issue" freezes its
     reminder count forever; a row marked "Resolved" that's still flagged
     reopens to Open (with a note) and gets +1. Each platform's rows are
     merged independently (only ever compared against its own prior rows)
     then combined into one rows list, tagged with a "platform" field so the
     dashboard can tell them apart.

     REMINDER CYCLES ARE CALENDAR-ANCHORED (added 2026-09-14, at Anika's
     request): "+1 reminder" above happens at most once per real week, not
     once per script run. CYCLE_ANCHOR = 2026-09-24 -- that week is cycle 1,
     the week after is cycle 2, and so on; every row now also carries
     "last_reminder_cycle" so a second run in the same week is a safe no-op
     instead of double-counting. Before 2026-09-24, current_cycle() is 0 and
     nothing increments at all. All platforms' reminders_sent were manually
     reset to 0 in docs/data.json on 2026-09-14 as part of this change, so
     counting genuinely starts clean from cycle 1.
  4. Writes the merged result to docs/data.json, verifies it round-trips
     (loads back and matches what was just written -- the project has been
     burned before by a "successful" write that wasn't), and commits +
     pushes it with git.
  5. Refuses to push if any platform's fresh data looks broken (see
     --min-rows / --max-drop-pct and their --nykaa-/--myntra- counterparts
     below) -- prints a clear error and exits non-zero instead, so nothing
     bad reaches Daksh.

Login: Flipkart requires you to log in by hand once in the browser this
scraper drives (flipkart_pdp_scraper.py --login), which saves a session
Playwright reuses for unattended runs. That session eventually expires --
when scrapes start failing, run --login again. Nothing in this pipeline
ever asks for or stores a password. Nykaa's, Myntra's, and Amazon's product
pages are all public -- no login step needed for any of them.

NYKAA/MYNTRA NEED --headed: confirmed (2026-09-11 for Nykaa, 2026-09-14 for
Myntra) that both sites block a headless Chromium outright
(net::ERR_HTTP2_PROTOCOL_ERROR on the very first request) but work fine with
a real, visible browser window. This script always launches both scrapes
with --headed, which means a browser window will actually pop up on screen
during those portions of the run -- that's expected, not a bug. It also
means this only works in an INTERACTIVE session: if your Task Scheduler task
is set to "Run whether user is logged on or not", the Nykaa and Myntra steps
will fail (there's no desktop for a visible window to appear on). Use "Run
only when user is logged on" instead, and make sure the machine is
unlocked/logged in at the scheduled time -- see SETUP.md.

AMAZON'S BOT-CHECK: confirmed (2026-09-14) that Amazon does NOT block
headless Chromium outright (unlike Nykaa/Myntra) -- amazon_pdp_scraper.py
runs headless by default. The real operational issue is a soft anti-bot
interstitial ("Click the button below to continue shopping") that shows up
occasionally, more tied to request pacing/behavior than a hard per-ASIN or
per-session ban -- the scraper detects and retries it automatically (see
amazon_pdp_scraper.py's BOT-CHECK FINDING), at a deliberately low
concurrency (2) with a longer delay between requests (3-6s) than Nykaa/
Myntra use. A handful of amazon_failures.csv rows after a full run is
expected, not a sign anything is broken -- see the printed failure count.

IMAGE COMPARISON (--visual, on by default as of 2026-09-15): Anika flagged a
real rebranded-packaging mismatch (D2C site still showing old packaging,
marketplaces showing the new one) that the dashboard never caught. Root
cause: qa_diff.py / nykaa_qa_diff.py / myntra_qa_diff.py / amazon_qa_diff.py
all have a perceptual-hash image comparison (visual_best_similarity /
visual_avg_similarity, feeding classify()'s P4 "Product Photo Mismatch"
tier) that's real and validated -- but it only runs behind each script's
--visual flag (needs network + the `pillow` package, and real extra time),
and this script never passed that flag through. So P4 had never fired once,
for any SKU, on any platform, since the pipeline went live -- not a scoring
bug, the check just never ran. Every diff_*() call now passes --visual by
default, so make sure `pillow` is installed (`pip install pillow`) in the
same Python environment this script runs in. Downloaded images are cached
in pipeline/qa_diff_image_cache/ (shared across all four platforms' diffs,
since the same D2C image gets reused across a SKU's platforms) so repeat
runs only download new/changed images -- that folder is gitignored, never
commit it. This adds real time to every run (downloading + hashing every
product's images from each platform's CDN and nathabit.in's); use
--skip-visual to fall back to the old (P4-blind) behavior if pillow isn't
set up yet or a CDN is being unreliable, rather than losing the whole run.

IMAGE COMPARISON, PART 2 -- COLOR (2026-09-15, same day): turning --visual on
above did NOT catch Anika's actual rebrand example. Real production diff
data for that SKU (FC-KL-CN-040 / FC-KL-CN2-040) scored visual_best_similarity
0.64-0.98 across all four platforms -- always at or above classify()'s 0.6
"same photo" threshold, so P4 still never fired for it. Root cause: the
perceptual hash in qa_diff.py's _phash() converts every image to grayscale
before comparing, so it is structurally blind to a pure color/branding
change -- confirmed synthetically (two gradient images built with the exact
same luminance profile but a completely different hue hashed at Hamming
distance 0, i.e. a "perfect" match). The old-vs-new packaging here is
mostly a color change (light pink -> dark maroon) with similar bottle
shape/studio lighting, which is exactly the case grayscale hashing cannot
see. Fix: qa_diff.py's visual_similarity() now also computes a color
signature per image (RGB, not grayscale -- see _color_signature) and
combines it with the existing shape hash via min(shape_sim, color_sim), so
an image pair only reads as "the same photo" when it matches on BOTH
dimensions. This is a change inside qa_diff.py itself (imported by
nykaa_qa_diff.py / myntra_qa_diff.py / amazon_qa_diff.py, not duplicated),
so all four platforms picked up the fix from one place -- no changes needed
here in weekly_refresh.py itself. Validated with qa_diff.py --selftest
(the rebrand case reproduced synthetically, plus a true-match regression
check) and end-to-end through qa_diff.run(..., do_visual=True) -> classify()
against a synthetic same-shape-different-color pair, confirming P4 now
fires (scored 0.455, well under the 0.6 threshold) where it previously
would not have.

IMAGE COMPARISON, PART 3 -- HERO-IMAGE ANCHORING (2026-09-17): the color fix
above still didn't move the real Amazon rebrand row (FC-KL-CN-040, ASIN
B0BK1SGP2M) -- it stayed at visual_best_similarity=0.984 after re-running
with the color-aware code confirmed live (checked via `git show
HEAD:pipeline/qa_diff.py`, so this wasn't a stale-file problem). Root cause,
found from that same row's visual_avg_similarity being only 0.679 (a big
gap from 0.984): qa_diff.py's visual_similarity() compared every
marketplace image against every D2C image (10 x 8 = 80 pairs here) and took
the single best pair found. Among that many pairs, one non-hero image --
almost certainly a shared asset like an ingredients graphic or a "how to
use" diagram, which platforms commonly reuse as the exact same file --
scored high enough to mask the real packaging photo mismatch entirely, even
though most of the gallery (reflected in the 0.679 average) genuinely
didn't match. Fix: visual_similarity() now only compares pairs involving at
least one side's HERO image (the first image in the gallery -- every
platform leads with the actual product/packaging shot), matched against the
OTHER side's entire gallery for its best match. This deliberately does NOT
require the two hero photos to match each other directly (different studio/
crop/angle would make that misfire on perfectly good listings) -- it keeps
all the existing crop/resize/re-encoding tolerance from _phash's autocrop
step, it just stops letting an unrelated shared graphic decide the score.
Validated with a selftest fixture that reproduces this exact shape (two
galleries, each with a genuinely mismatched hero image AND an identical
decoy image shared between them) -- confirmed the decoy no longer masks the
mismatch, and a real hero-to-hero match still scores high with an unrelated
decoy present in both galleries. Again a change entirely inside qa_diff.py,
so all four platforms picked it up from one place.

IMAGE COMPARISON, PART 4 -- BACKGROUND COLOR (2026-09-17, same day): the
hero-anchoring fix above moved the real Amazon rebrand row from 0.984 down
to 0.656 -- real progress, confirmed via the histogram-based sanity check
below, but still just above the 0.6 threshold. Rather than just nudge the
threshold, Anika spot-checked several SKUs that the fixes so far had newly
started flagging: some were genuine rebrand mismatches, but a couple had
IDENTICAL packaging and were only flagged because they'd been photographed
against a different-colored studio backdrop (e.g. plain white on one
platform, a warm/cream background on another). Root cause: qa_diff.py's
_color_signature() averaged color across the WHOLE photo frame, and a
plain background typically fills most of a product photo -- so its color
alone could shift the average enough to look like a packaging difference,
even with the product itself unchanged. Fixed by excluding background-
like pixels (near-white/near-gray, low-saturation, high-brightness -- see
_is_backgroundish()) before averaging, so the signature reflects the
product's own color, not the backdrop. Validated with a selftest fixture
of the exact case Anika found (identical product color, two different
plain backgrounds) confirming it now reads as a match, plus a regression
check confirming a genuine packaging color change on an unchanged
background is still caught. As with the other three image-comparison
fixes, this lives entirely in qa_diff.py's _color_signature(), so all four
platforms picked it up from one place.

Before trusting ANY of these thresholds in production, also see
pipeline/visual_similarity_histogram.py -- a small diagnostic that buckets
every platform's visual_best_similarity into ranges from the already-
generated diff CSVs (no network needed). Run it after a diff to sanity-
check that scores are behaving as expected before assuming a given
threshold is catching real mismatches without an unacceptable false-
positive rate; this is what surfaced the background-color problem above in
the first place (a smooth, unseparated distribution rather than a clean
"genuine matches near 1.0, real mismatches near 0" split was the tell that
something was still off).

Usage (from the repo root, with the pipeline/ scripts and a clone of this
same GitHub repo both available):

    python pipeline/weekly_refresh.py --repo-dir /path/to/local/clone

See SETUP.md at the repo root for the one-time Task Scheduler setup.
"""
import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPELINE_DIR))

from build_kam_review import (  # noqa: E402
    classify, classify_nykaa, classify_myntra, classify_amazon, load_diff_rows,
    DIFF_COLS_TYPES, NYKAA_DIFF_COLS_TYPES, MYNTRA_DIFF_COLS_TYPES, AMAZON_DIFF_COLS_TYPES,
)


def run(cmd, cwd=None):
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        raise SystemExit(f"Command failed ({result.returncode}): {' '.join(cmd)}")


def scrape_flipkart(pipeline_dir, flipkart_out, limit=None):
    fk_cmd = [sys.executable, str(pipeline_dir / "flipkart_pdp_scraper.py"), "--out", str(flipkart_out)]
    if limit:
        fk_cmd += ["--limit", str(limit)]
    run(fk_cmd, cwd=pipeline_dir)


def scrape_d2c(pipeline_dir, d2c_out, limit=None):
    # The D2C (nathabit.in) reference is shared by every platform's diff --
    # Nykaa and Myntra need it just as much as Flipkart does, so this runs
    # even on a Flipkart-skipped (Nykaa-only/Myntra-only) smoke test.
    d2c_cmd = [sys.executable, str(pipeline_dir / "nathabit_pdp_scraper.py"), "--out", str(d2c_out)]
    if limit:
        d2c_cmd += ["--limit", str(limit)]
    run(d2c_cmd, cwd=pipeline_dir)


def scrape_nykaa(pipeline_dir, nykaa_worklist, nykaa_out, nykaa_fail, limit=None):
    # --headed is NOT optional -- see the module docstring's "NYKAA NEEDS
    # --headed" section. This requires an interactive desktop session.
    cmd = [sys.executable, str(pipeline_dir / "nykaa_pdp_scraper.py"),
           "--worklist", str(nykaa_worklist), "--out", str(nykaa_out),
           "--failures", str(nykaa_fail), "--headed"]
    if limit:
        cmd += ["--limit", str(limit)]
    run(cmd, cwd=pipeline_dir)


def diff(pipeline_dir, flipkart_csv, d2c_csv, diff_out, do_visual=True):
    cmd = [sys.executable, str(pipeline_dir / "qa_diff.py"),
           "--flipkart", str(flipkart_csv), "--d2c", str(d2c_csv), "--out", str(diff_out)]
    if do_visual:
        cmd.append("--visual")
    run(cmd, cwd=pipeline_dir)


def diff_nykaa(pipeline_dir, nykaa_csv, d2c_csv, diff_out, do_visual=True):
    cmd = [sys.executable, str(pipeline_dir / "nykaa_qa_diff.py"),
           "--nykaa", str(nykaa_csv), "--d2c", str(d2c_csv), "--out", str(diff_out)]
    if do_visual:
        cmd.append("--visual")
    run(cmd, cwd=pipeline_dir)


def scrape_myntra(pipeline_dir, myntra_worklist, myntra_out, myntra_fail, limit=None):
    # --headed is NOT optional -- see the module docstring's "NYKAA/MYNTRA
    # NEED --headed" section. This requires an interactive desktop session.
    cmd = [sys.executable, str(pipeline_dir / "myntra_pdp_scraper.py"),
           "--worklist", str(myntra_worklist), "--out", str(myntra_out),
           "--failures", str(myntra_fail)]
    if limit:
        cmd += ["--limit", str(limit)]
    run(cmd, cwd=pipeline_dir)


def diff_myntra(pipeline_dir, myntra_csv, d2c_csv, diff_out, do_visual=True):
    cmd = [sys.executable, str(pipeline_dir / "myntra_qa_diff.py"),
           "--myntra", str(myntra_csv), "--d2c", str(d2c_csv), "--out", str(diff_out)]
    if do_visual:
        cmd.append("--visual")
    run(cmd, cwd=pipeline_dir)


def scrape_amazon(pipeline_dir, amazon_worklist, amazon_out, amazon_fail, limit=None):
    # No --headed here -- unlike Nykaa/Myntra, Amazon does not block headless
    # Chromium (see the module docstring's "AMAZON'S BOT-CHECK" section).
    # amazon_pdp_scraper.py already defaults to headless and to a lower
    # concurrency / longer delay than the other scrapers to go easy on
    # Amazon's bot-check.
    cmd = [sys.executable, str(pipeline_dir / "amazon_pdp_scraper.py"),
           "--worklist", str(amazon_worklist), "--out", str(amazon_out),
           "--failures", str(amazon_fail)]
    if limit:
        cmd += ["--limit", str(limit)]
    run(cmd, cwd=pipeline_dir)


def diff_amazon(pipeline_dir, amazon_csv, d2c_csv, diff_out, do_visual=True):
    cmd = [sys.executable, str(pipeline_dir / "amazon_qa_diff.py"),
           "--amazon", str(amazon_csv), "--d2c", str(d2c_csv), "--out", str(diff_out)]
    if do_visual:
        cmd.append("--visual")
    run(cmd, cwd=pipeline_dir)


def load_prior_rows(data_json_path):
    """Returns {(platform, nh_sku, platform_id): row_dict} from the current
    docs/data.json, or {} if it doesn't exist yet. Rows written before the
    "platform" field existed (every row so far has been Flipkart) are
    treated as platform="flipkart", platform_id=their "fsn" -- so old data
    keeps merging forward correctly with no migration step needed."""
    if not data_json_path.exists():
        return {}
    with open(data_json_path, encoding="utf-8") as f:
        payload = json.load(f)
    out = {}
    for r in payload.get("rows", []):
        platform = r.get("platform", "flipkart")
        platform_id = r.get("platform_id") or r.get("fsn") or r.get("nykaa_product_id", "")
        out[(platform, r["nh_sku"], platform_id)] = r
    return out


# Reminder cycles are calendar-anchored, not run-anchored: reminders_sent
# used to bump by 1 every time this script ran and found a row still open,
# which meant a SKU could get "reminded" more than once in the same real
# week if the script happened to run more than once that week (as it did
# repeatedly during Myntra's rollout/testing) -- so the number on the
# dashboard ended up reflecting how many times the pipeline had been run
# (roughly tracking how much SKU/testing churn there'd been), not how many
# real weekly reminders had gone out. CYCLE_ANCHOR fixes that: cycle 1 is
# the week starting 2026-09-24 (Anika's requested restart date), cycle 2 the
# week after, etc, and reminders_sent now increments AT MOST ONCE per cycle
# no matter how many times weekly_refresh.py actually runs inside it. Every
# row also carries "last_reminder_cycle" so a second run in the same week is
# a safe no-op. Before 2026-09-24, current_cycle() is 0 and nothing
# increments at all -- see Anika's request on 2026-09-14 to reset every
# platform's reminders_sent to 0 and hold the count there until the 24th.
CYCLE_ANCHOR = datetime.date(2026, 9, 24)


def current_cycle(today=None):
    """Weekly reminder-cycle number counting from CYCLE_ANCHOR. Returns 0 for
    any date before the anchor (tracking hasn't started -- no cycle has been
    sent yet), 1 for the anchor week itself, 2 for the week after, etc."""
    today = today or datetime.date.today()
    if today < CYCLE_ANCHOR:
        return 0
    return (today - CYCLE_ANCHOR).days // 7 + 1


def merge(diff_csv_path, prior_rows, platform):
    """Same merge-forward rules as build_kam_review.build(), operating on
    dicts instead of an xlsx sheet. platform is "flipkart", "nykaa",
    "myntra", or "amazon" -- picks the right diff-column types and classify
    function, and tags every row with a "platform" field so multiple
    platforms can share one docs/data.json without colliding. Flipkart's
    doc_id format (nh_sku__fsn) is left exactly as it always was, for a clean
    history; Nykaa, Myntra, and Amazon each get a distinct format
    (nh_sku__nykaa__product_id, nh_sku__myntra__style_id,
    nh_sku__amazon__asin) that can never collide with a Flipkart FSN or each
    other. Amazon is the one platform where the same nh_sku can legitimately
    appear more than once (13 SKUs have 2+ simultaneously-Active ASINs, kept
    as separate rows in amazon_worklist.csv) -- the nh_sku__amazon__asin key
    already handles that correctly since asin makes each key unique."""
    if platform == "flipkart":
        diff_rows = load_diff_rows(str(diff_csv_path), DIFF_COLS_TYPES)
        classify_fn = classify
        id_col, title_col = "fsn", "flipkart_title"
    elif platform == "nykaa":
        diff_rows = load_diff_rows(str(diff_csv_path), NYKAA_DIFF_COLS_TYPES)
        classify_fn = classify_nykaa
        id_col, title_col = "nykaa_product_id", "nykaa_title"
    elif platform == "myntra":
        diff_rows = load_diff_rows(str(diff_csv_path), MYNTRA_DIFF_COLS_TYPES)
        classify_fn = classify_myntra
        id_col, title_col = "style_id", "myntra_title"
    elif platform == "amazon":
        diff_rows = load_diff_rows(str(diff_csv_path), AMAZON_DIFF_COLS_TYPES)
        classify_fn = classify_amazon
        id_col, title_col = "asin", "amazon_title"
    else:
        raise ValueError(f"unknown platform: {platform!r}")

    classified = []
    stats = {"new": 0, "carried_open": 0, "frozen_not_an_issue": 0, "reopened": 0}

    platform_prior_keys = {k for k in prior_rows if k[0] == platform}
    seen_keys = set()
    cyc = current_cycle()

    for rr in diff_rows:
        tier, issue, detail = classify_fn(rr)
        if not tier:
            continue
        platform_id = rr.get(id_col, "")
        key = (platform, rr["nh_sku"], platform_id)
        seen_keys.add(key)
        prior = prior_rows.get(key)
        if prior is None:
            # First time this SKU has ever been flagged -- doesn't count as
            # a reminder itself, but its baseline cycle is set to the
            # current one so it isn't immediately eligible for another
            # increment if this script runs again later in the same week.
            status, reminders, last_cycle = "Open", 0, cyc
            stats["new"] += 1
        elif prior["status"] == "Not an Issue":
            status, reminders = "Not an Issue", prior["reminders_sent"]
            last_cycle = prior.get("last_reminder_cycle", 0)
            stats["frozen_not_an_issue"] += 1
        else:
            prior_cycle = prior.get("last_reminder_cycle", 0)
            # Only bump reminders_sent if we've reached cycle 1 (2026-09-24
            # or later) AND this is a new cycle for this row -- a same-week
            # rerun (or any run before the anchor date) is a no-op here.
            new_cycle_reached = cyc >= 1 and cyc > prior_cycle
            if prior["status"] == "Resolved":
                reminders = prior["reminders_sent"] + 1 if new_cycle_reached else prior["reminders_sent"]
                last_cycle = cyc if new_cycle_reached else prior_cycle
                detail = detail + " [recurred after being marked Resolved]"
                stats["reopened"] += 1
            else:  # still Open
                reminders = prior["reminders_sent"] + 1 if new_cycle_reached else prior["reminders_sent"]
                last_cycle = cyc if new_cycle_reached else prior_cycle
                stats["carried_open"] += 1
            status = "Open"

        if platform == "flipkart":
            doc_id = f"{rr['nh_sku']}__{platform_id}"
        else:
            doc_id = f"{rr['nh_sku']}__{platform}__{platform_id}"

        classified.append({
            "doc_id": doc_id,
            "platform": platform,
            "priority": tier,
            "status": status,
            "nh_sku": rr["nh_sku"],
            "platform_id": platform_id,
            # Kept for backward compatibility with the dashboard's existing
            # Flipkart-specific rendering/search code -- blank for the other
            # platforms' rows.
            "fsn": platform_id if platform == "flipkart" else "",
            "nykaa_product_id": platform_id if platform == "nykaa" else "",
            "issue": issue,
            "detail": detail,
            "marketplace_title": rr.get(title_col, ""),
            "flipkart_title": rr.get(title_col, "") if platform == "flipkart" else "",
            "nykaa_title": rr.get(title_col, "") if platform == "nykaa" else "",
            "myntra_title": rr.get(title_col, "") if platform == "myntra" else "",
            "amazon_title": rr.get(title_col, "") if platform == "amazon" else "",
            "d2c_title": rr.get("d2c_title", ""),
            "reminders_sent": reminders,
            "last_reminder_cycle": last_cycle,
            "status_changed_at": (prior or {}).get("status_changed_at"),
        })

    classified.sort(key=lambda r: (r["priority"], r["nh_sku"]))
    dropped = len(platform_prior_keys - seen_keys)
    stats["dropped"] = max(dropped, 0)
    return classified, stats


def sanity_check(rows, prior_rows, min_rows, max_drop_pct):
    """Refuse to publish data that looks broken -- e.g. the scrape mostly failed."""
    if len(rows) < min_rows:
        return f"Only {len(rows)} flagged rows found (expected at least {min_rows}) -- looks like the scrape mostly failed."
    if prior_rows:
        drop_pct = 1 - (len(rows) / max(len(prior_rows), 1))
        if drop_pct > max_drop_pct:
            return (f"Row count dropped from {len(prior_rows)} to {len(rows)} "
                    f"({drop_pct:.0%} drop, threshold is {max_drop_pct:.0%}) -- looks suspicious, not a real improvement.")
    return None


def write_data_json(rows, out_path):
    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": "weekly_refresh.py",
        "rows": rows,
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    out_path.write_text(text, encoding="utf-8")

    # Verify it round-trips before trusting it -- burned before by a "successful"
    # write that silently wasn't (see project notes on the Drive-upload corruption).
    reloaded = json.loads(out_path.read_text(encoding="utf-8"))
    if reloaded["rows"] != rows:
        raise SystemExit("data.json did not round-trip correctly after writing -- aborting, nothing was committed.")
    return payload


def _format_stats(label, stats):
    return (f"{label}: {stats['new']} new, {stats['carried_open']} carried open, "
            f"{stats['frozen_not_an_issue']} frozen (not an issue), {stats['reopened']} reopened, "
            f"{stats['dropped']} dropped")


def git_commit_and_push(repo_dir, data_json_relpath, stats_by_platform):
    message = "Weekly refresh: " + " | ".join(
        _format_stats(platform, stats) for platform, stats in stats_by_platform.items()
    )
    run(["git", "add", data_json_relpath], cwd=repo_dir)
    status = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_dir)
    if status.returncode == 0:
        print("No changes to commit (data.json is identical to the last run).")
        return
    run(["git", "commit", "-m", message], cwd=repo_dir)
    run(["git", "push"], cwd=repo_dir)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-dir", required=True, help="local clone of the GitHub repo (the one with docs/data.json)")
    ap.add_argument("--pipeline-dir", default=str(PIPELINE_DIR), help="directory containing the scraper/diff scripts")
    ap.add_argument("--data-path", default="docs/data.json", help="path to data.json, relative to --repo-dir")
    ap.add_argument("--min-rows", type=int, default=30, help="Flipkart: abort if fewer than this many rows are flagged")
    ap.add_argument("--max-drop-pct", type=float, default=0.5, help="Flipkart: abort if row count drops by more than this fraction week over week")
    ap.add_argument("--nykaa-min-rows", type=int, default=5,
                     help="Nykaa: abort if fewer than this many rows are flagged (lower default than "
                          "Flipkart's -- Nykaa's catalog is smaller (~443 SKUs vs ~640) and this pipeline "
                          "is new, with no full-catalog run yet to calibrate a realistic baseline against)")
    ap.add_argument("--nykaa-max-drop-pct", type=float, default=0.5, help="Nykaa: abort if row count drops by more than this fraction week over week")
    ap.add_argument("--myntra-min-rows", type=int, default=5,
                     help="Myntra: abort if fewer than this many rows are flagged (same low default as "
                          "Nykaa's -- this pipeline is brand new, with no full-catalog run yet to "
                          "calibrate a realistic baseline against)")
    ap.add_argument("--myntra-max-drop-pct", type=float, default=0.5, help="Myntra: abort if row count drops by more than this fraction week over week")
    ap.add_argument("--amazon-min-rows", type=int, default=5,
                     help="Amazon: abort if fewer than this many rows are flagged (same low default as "
                          "Nykaa/Myntra -- this pipeline is brand new, with no full-catalog run yet to "
                          "calibrate a realistic baseline against)")
    ap.add_argument("--amazon-max-drop-pct", type=float, default=0.5, help="Amazon: abort if row count drops by more than this fraction week over week")
    ap.add_argument("--limit", type=int, help="scrape only the first N products per platform (smoke test)")
    ap.add_argument("--skip-scrape", action="store_true", help="reuse existing observed CSVs instead of scraping again (for testing)")
    ap.add_argument("--skip-flipkart", action="store_true",
                     help="skip the Flipkart scrape+diff entirely -- use this for a Nykaa-only or "
                          "Myntra-only smoke test so a Flipkart login session isn't required just to "
                          "test another platform")
    ap.add_argument("--skip-nykaa", action="store_true",
                     help="skip the Nykaa scrape+diff entirely -- use this for a Flipkart-only run, e.g. "
                          "if this is running non-interactively and Nykaa's required --headed browser "
                          "window can't be shown (see the module docstring's NYKAA/MYNTRA NEED --headed section)")
    ap.add_argument("--skip-myntra", action="store_true",
                     help="skip the Myntra scrape+diff entirely -- same reasoning as --skip-nykaa "
                          "(Myntra also needs a visible --headed browser window)")
    ap.add_argument("--skip-amazon", action="store_true",
                     help="skip the Amazon scrape+diff entirely -- useful for a Flipkart/Nykaa/Myntra-only "
                          "run, or if Amazon's bot-check is being unusually aggressive that day")
    ap.add_argument("--nykaa-worklist", default=None,
                     help="path to nykaa_worklist.csv (default: <pipeline-dir>/nykaa_worklist.csv)")
    ap.add_argument("--myntra-worklist", default=None,
                     help="path to myntra_worklist.csv (default: <pipeline-dir>/myntra_worklist.csv)")
    ap.add_argument("--amazon-worklist", default=None,
                     help="path to amazon_worklist.csv (default: <pipeline-dir>/amazon_worklist.csv)")
    ap.add_argument("--skip-visual", action="store_true",
                     help="skip the perceptual image comparison (--visual) that all four diff scripts now run "
                          "by default -- it downloads every product image from each platform's CDN and "
                          "nathabit.in, so it needs network access and the `pillow` package (pip install "
                          "pillow), and adds real time to the run (images are cached in "
                          "pipeline/qa_diff_image_cache/ so a re-run doesn't re-download unchanged ones). "
                          "Use this flag if pillow isn't installed yet, or the image CDNs are being flaky, "
                          "rather than losing the whole run -- P4 (Product Photo Mismatch) just won't fire "
                          "on that run's rows if you skip it, same as every run before 2026-09-15 did.")
    args = ap.parse_args()

    repo_dir = Path(args.repo_dir).resolve()
    pipeline_dir = Path(args.pipeline_dir).resolve()
    data_json_path = repo_dir / args.data_path

    flipkart_csv = pipeline_dir / "observed_flipkart_latest.csv"
    d2c_csv = pipeline_dir / "nathabit_reference_latest.csv"
    diff_csv = pipeline_dir / "qa_diff_latest.csv"

    nykaa_worklist = Path(args.nykaa_worklist) if args.nykaa_worklist else pipeline_dir / "nykaa_worklist.csv"
    nykaa_csv = pipeline_dir / "observed_nykaa_latest.csv"
    nykaa_fail_csv = pipeline_dir / "nykaa_failures.csv"
    nykaa_diff_csv = pipeline_dir / "nykaa_diff_latest.csv"

    myntra_worklist = Path(args.myntra_worklist) if args.myntra_worklist else pipeline_dir / "myntra_worklist.csv"
    myntra_csv = pipeline_dir / "observed_myntra_latest.csv"
    myntra_fail_csv = pipeline_dir / "myntra_failures.csv"
    myntra_diff_csv = pipeline_dir / "myntra_diff_latest.csv"

    amazon_worklist = Path(args.amazon_worklist) if args.amazon_worklist else pipeline_dir / "amazon_worklist.csv"
    amazon_csv = pipeline_dir / "observed_amazon_latest.csv"
    amazon_fail_csv = pipeline_dir / "amazon_failures.csv"
    amazon_diff_csv = pipeline_dir / "amazon_diff_latest.csv"

    if not args.skip_scrape:
        scrape_d2c(pipeline_dir, d2c_csv, limit=args.limit)
        if not args.skip_flipkart:
            scrape_flipkart(pipeline_dir, flipkart_csv, limit=args.limit)
        if not args.skip_nykaa:
            scrape_nykaa(pipeline_dir, nykaa_worklist, nykaa_csv, nykaa_fail_csv, limit=args.limit)
        if not args.skip_myntra:
            scrape_myntra(pipeline_dir, myntra_worklist, myntra_csv, myntra_fail_csv, limit=args.limit)
        if not args.skip_amazon:
            scrape_amazon(pipeline_dir, amazon_worklist, amazon_csv, amazon_fail_csv, limit=args.limit)
    do_visual = not args.skip_visual
    if not args.skip_flipkart:
        diff(pipeline_dir, flipkart_csv, d2c_csv, diff_csv, do_visual=do_visual)
    if not args.skip_nykaa:
        diff_nykaa(pipeline_dir, nykaa_csv, d2c_csv, nykaa_diff_csv, do_visual=do_visual)
    if not args.skip_myntra:
        diff_myntra(pipeline_dir, myntra_csv, d2c_csv, myntra_diff_csv, do_visual=do_visual)
    if not args.skip_amazon:
        diff_amazon(pipeline_dir, amazon_csv, d2c_csv, amazon_diff_csv, do_visual=do_visual)

    cyc = current_cycle()
    if cyc == 0:
        print(f"\nReminder cycle: none yet -- cycles start the week of {CYCLE_ANCHOR.isoformat()}. "
              f"reminders_sent will NOT increment on this run.")
    else:
        print(f"\nReminder cycle: {cyc} (week of {(CYCLE_ANCHOR + datetime.timedelta(days=(cyc - 1) * 7)).isoformat()}). "
              f"A row already reminded this cycle will not be double-counted if this script runs again before the next cycle.")

    prior_rows = load_prior_rows(data_json_path)
    if args.skip_flipkart:
        fk_rows, fk_stats = [], {"new": 0, "carried_open": 0, "frozen_not_an_issue": 0, "reopened": 0, "dropped": 0}
        # Keep whatever Flipkart rows are already in docs/data.json untouched --
        # a Nykaa-only/Myntra-only smoke test must not silently wipe them out.
        fk_rows = [r for r in prior_rows.values() if r.get("platform", "flipkart") == "flipkart"]
    else:
        fk_rows, fk_stats = merge(diff_csv, prior_rows, "flipkart")
    if args.skip_nykaa:
        nk_rows, nk_stats = [], {"new": 0, "carried_open": 0, "frozen_not_an_issue": 0, "reopened": 0, "dropped": 0}
        # Keep whatever Nykaa rows are already in docs/data.json untouched --
        # a Flipkart-only run must not silently wipe out Nykaa's rows.
        nk_rows = [r for r in prior_rows.values() if r.get("platform") == "nykaa"]
    else:
        nk_rows, nk_stats = merge(nykaa_diff_csv, prior_rows, "nykaa")
    if args.skip_myntra:
        my_rows, my_stats = [], {"new": 0, "carried_open": 0, "frozen_not_an_issue": 0, "reopened": 0, "dropped": 0}
        # Same reasoning as --skip-nykaa above -- don't wipe out Myntra's
        # rows just because this run skipped it.
        my_rows = [r for r in prior_rows.values() if r.get("platform") == "myntra"]
    else:
        my_rows, my_stats = merge(myntra_diff_csv, prior_rows, "myntra")
    if args.skip_amazon:
        az_rows, az_stats = [], {"new": 0, "carried_open": 0, "frozen_not_an_issue": 0, "reopened": 0, "dropped": 0}
        # Same reasoning as --skip-nykaa/--skip-myntra above -- don't wipe
        # out Amazon's rows just because this run skipped it.
        az_rows = [r for r in prior_rows.values() if r.get("platform") == "amazon"]
    else:
        az_rows, az_stats = merge(amazon_diff_csv, prior_rows, "amazon")
    rows = fk_rows + nk_rows + my_rows + az_rows
    stats_by_platform = {"flipkart": fk_stats, "nykaa": nk_stats, "myntra": my_stats, "amazon": az_stats}

    if args.limit:
        # A --limit run only ever looks at part of the catalog, so `rows` here
        # reflects just that slice -- it must never overwrite the real
        # data.json (which holds every currently-flagged product), or a
        # smoke test silently wipes out everyone else's open issues. Write
        # to a throwaway file instead, and skip the sanity checks below --
        # they're calibrated for full-catalog runs and will always look
        # like a "failure" against a partial one.
        test_out = data_json_path.with_name(data_json_path.stem + ".test" + data_json_path.suffix)
        write_data_json(rows, test_out)
        print(f"\n--limit {args.limit} was set, so this only scraped part of the catalog.")
        print(f"Wrote {len(rows)} test rows to {test_out} for inspection -- "
              f"the live {args.data_path} was left untouched and nothing was pushed.")
        print(f"Stats: flipkart={fk_stats} | nykaa={nk_stats} | myntra={my_stats} | amazon={az_stats}")
        print("This confirms the scrape + diff + merge steps work end to end. "
              "Run again WITHOUT --limit for a real, full-catalog refresh.")
        return

    if not args.skip_flipkart:
        fk_prior = {k: v for k, v in prior_rows.items() if k[0] == "flipkart"}
        problem = sanity_check(fk_rows, fk_prior, args.min_rows, args.max_drop_pct)
        if problem:
            print(f"\nABORTING (flipkart) -- {problem}\nNothing was written or pushed. Check the scrape output before retrying.\n", file=sys.stderr)
            raise SystemExit(1)

    if not args.skip_nykaa:
        nk_prior = {k: v for k, v in prior_rows.items() if k[0] == "nykaa"}
        problem = sanity_check(nk_rows, nk_prior, args.nykaa_min_rows, args.nykaa_max_drop_pct)
        if problem:
            print(f"\nABORTING (nykaa) -- {problem}\nNothing was written or pushed. Check the scrape output before retrying.\n", file=sys.stderr)
            raise SystemExit(1)

    if not args.skip_myntra:
        my_prior = {k: v for k, v in prior_rows.items() if k[0] == "myntra"}
        problem = sanity_check(my_rows, my_prior, args.myntra_min_rows, args.myntra_max_drop_pct)
        if problem:
            print(f"\nABORTING (myntra) -- {problem}\nNothing was written or pushed. Check the scrape output before retrying.\n", file=sys.stderr)
            raise SystemExit(1)

    if not args.skip_amazon:
        az_prior = {k: v for k, v in prior_rows.items() if k[0] == "amazon"}
        problem = sanity_check(az_rows, az_prior, args.amazon_min_rows, args.amazon_max_drop_pct)
        if problem:
            print(f"\nABORTING (amazon) -- {problem}\nNothing was written or pushed. Check the scrape output before retrying.\n", file=sys.stderr)
            raise SystemExit(1)

    write_data_json(rows, data_json_path)
    print(f"Wrote {len(rows)} rows to {data_json_path} ({len(fk_rows)} flipkart, {len(nk_rows)} nykaa, {len(my_rows)} myntra, {len(az_rows)} amazon)")
    print(f"Stats: flipkart={fk_stats} | nykaa={nk_stats} | myntra={my_stats} | amazon={az_stats}")

    git_commit_and_push(repo_dir, args.data_path, stats_by_platform)
    print("Done.")


if __name__ == "__main__":
    main()
