#!/usr/bin/env python3
"""Weekly refresh for the Listing QA KAM dashboard (Flipkart + Nykaa).

Run this on Anika's Windows machine (via Task Scheduler) once a week. It:

  1. Scrapes current Flipkart + Nykaa + nathabit.in listing data (reusing a
     browser session that was already logged in manually for Flipkart --
     see "Login" below; Nykaa needs no login, but DOES need a real, visible
     browser window -- see "Nykaa needs --headed" below).
  2. Diffs each platform against the same nathabit.in D2C reference
     (qa_diff.py for Flipkart, nykaa_qa_diff.py for Nykaa) and classifies
     the results into priority tiers (classify()/classify_nykaa() from
     build_kam_review.py, imported here so the three never drift apart).
  3. Merges the fresh results, per platform, against whatever is CURRENTLY
     in the repo's docs/data.json -- same merge-forward rules as before: a
     brand new row starts at Open/0 reminders; a row still Open and still
     flagged gets +1 reminder; a row marked "Not an Issue" freezes its
     reminder count forever; a row marked "Resolved" that's still flagged
     reopens to Open (with a note) and gets +1. Flipkart and Nykaa rows are
     merged independently (each platform's rows are only ever compared
     against its own prior rows) then combined into one rows list, tagged
     with a "platform" field so the dashboard can tell them apart.
  4. Writes the merged result to docs/data.json, verifies it round-trips
     (loads back and matches what was just written -- the project has been
     burned before by a "successful" write that wasn't), and commits +
     pushes it with git.
  5. Refuses to push if either platform's fresh data looks broken (see
     --min-rows / --max-drop-pct below) -- prints a clear error and exits
     non-zero instead, so nothing bad reaches Daksh.

Login: Flipkart requires you to log in by hand once in the browser this
scraper drives (flipkart_pdp_scraper.py --login), which saves a session
Playwright reuses for unattended runs. That session eventually expires --
when scrapes start failing, run --login again. Nothing in this pipeline
ever asks for or stores a password. Nykaa's product pages are public --
no login step needed for it at all.

NYKAA NEEDS --headed: confirmed (2026-09-11) that Nykaa blocks a headless
Chromium outright (every request failed with net::ERR_HTTP2_PROTOCOL_ERROR/
a timeout) but works fine with a real, visible browser window. This script
always launches Nykaa's scrape with --headed, which means a browser window
will actually pop up on screen during the Nykaa portion of the run --
that's expected, not a bug. It also means this only works in an
INTERACTIVE session: if your Task Scheduler task is set to "Run whether
user is logged on or not", the Nykaa step will fail (there's no desktop for
a visible window to appear on). Use "Run only when user is logged on"
instead, and make sure the machine is unlocked/logged in at the scheduled
time -- see SETUP.md.

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
    classify, classify_nykaa, load_diff_rows,
    DIFF_COLS_TYPES, NYKAA_DIFF_COLS_TYPES,
)


def run(cmd, cwd=None):
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        raise SystemExit(f"Command failed ({result.returncode}): {' '.join(cmd)}")


def scrape(pipeline_dir, flipkart_out, d2c_out, limit=None):
    fk_cmd = [sys.executable, str(pipeline_dir / "flipkart_pdp_scraper.py"), "--out", str(flipkart_out)]
    d2c_cmd = [sys.executable, str(pipeline_dir / "nathabit_pdp_scraper.py"), "--out", str(d2c_out)]
    if limit:
        fk_cmd += ["--limit", str(limit)]
        d2c_cmd += ["--limit", str(limit)]
    run(fk_cmd, cwd=pipeline_dir)
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


def diff(pipeline_dir, flipkart_csv, d2c_csv, diff_out):
    run([sys.executable, str(pipeline_dir / "qa_diff.py"),
         "--flipkart", str(flipkart_csv), "--d2c", str(d2c_csv), "--out", str(diff_out)],
        cwd=pipeline_dir)


def diff_nykaa(pipeline_dir, nykaa_csv, d2c_csv, diff_out):
    run([sys.executable, str(pipeline_dir / "nykaa_qa_diff.py"),
         "--nykaa", str(nykaa_csv), "--d2c", str(d2c_csv), "--out", str(diff_out)],
        cwd=pipeline_dir)


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


def merge(diff_csv_path, prior_rows, platform):
    """Same merge-forward rules as build_kam_review.build(), operating on
    dicts instead of an xlsx sheet. platform is "flipkart" or "nykaa" --
    picks the right diff-column types and classify function, and tags every
    row with a "platform" field so multiple platforms can share one
    docs/data.json without colliding. Flipkart's doc_id format
    (nh_sku__fsn) is left exactly as it always was, for a clean history;
    Nykaa gets a distinct format (nh_sku__nykaa__product_id) that can never
    collide with a Flipkart FSN."""
    if platform == "flipkart":
        diff_rows = load_diff_rows(str(diff_csv_path), DIFF_COLS_TYPES)
        classify_fn = classify
        id_col, title_col = "fsn", "flipkart_title"
    elif platform == "nykaa":
        diff_rows = load_diff_rows(str(diff_csv_path), NYKAA_DIFF_COLS_TYPES)
        classify_fn = classify_nykaa
        id_col, title_col = "nykaa_product_id", "nykaa_title"
    else:
        raise ValueError(f"unknown platform: {platform!r}")

    classified = []
    stats = {"new": 0, "carried_open": 0, "frozen_not_an_issue": 0, "reopened": 0}

    platform_prior_keys = {k for k in prior_rows if k[0] == platform}
    seen_keys = set()

    for rr in diff_rows:
        tier, issue, detail = classify_fn(rr)
        if not tier:
            continue
        platform_id = rr.get(id_col, "")
        key = (platform, rr["nh_sku"], platform_id)
        seen_keys.add(key)
        prior = prior_rows.get(key)
        if prior is None:
            status, reminders = "Open", 0
            stats["new"] += 1
        elif prior["status"] == "Not an Issue":
            status, reminders = "Not an Issue", prior["reminders_sent"]
            stats["frozen_not_an_issue"] += 1
        elif prior["status"] == "Resolved":
            status, reminders = "Open", prior["reminders_sent"] + 1
            detail = detail + " [recurred after being marked Resolved]"
            stats["reopened"] += 1
        else:  # still Open
            status, reminders = "Open", prior["reminders_sent"] + 1
            stats["carried_open"] += 1

        if platform == "flipkart":
            doc_id = f"{rr['nh_sku']}__{platform_id}"
        else:
            doc_id = f"{rr['nh_sku']}__nykaa__{platform_id}"

        classified.append({
            "doc_id": doc_id,
            "platform": platform,
            "priority": tier,
            "status": status,
            "nh_sku": rr["nh_sku"],
            "platform_id": platform_id,
            # Kept for backward compatibility with the dashboard's existing
            # Flipkart-specific rendering/search code -- blank for the other
            # platform's rows.
            "fsn": platform_id if platform == "flipkart" else "",
            "nykaa_product_id": platform_id if platform == "nykaa" else "",
            "issue": issue,
            "detail": detail,
            "marketplace_title": rr.get(title_col, ""),
            "flipkart_title": rr.get(title_col, "") if platform == "flipkart" else "",
            "nykaa_title": rr.get(title_col, "") if platform == "nykaa" else "",
            "d2c_title": rr.get("d2c_title", ""),
            "reminders_sent": reminders,
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
    ap.add_argument("--limit", type=int, help="scrape only the first N products per platform (smoke test)")
    ap.add_argument("--skip-scrape", action="store_true", help="reuse existing observed CSVs instead of scraping again (for testing)")
    ap.add_argument("--skip-nykaa", action="store_true",
                     help="skip the Nykaa scrape+diff entirely -- use this for a Flipkart-only run, e.g. "
                          "if this is running non-interactively and Nykaa's required --headed browser "
                          "window can't be shown (see the module docstring's NYKAA NEEDS --headed section)")
    ap.add_argument("--nykaa-worklist", default=None,
                     help="path to nykaa_worklist.csv (default: <pipeline-dir>/nykaa_worklist.csv)")
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

    if not args.skip_scrape:
        scrape(pipeline_dir, flipkart_csv, d2c_csv, limit=args.limit)
        if not args.skip_nykaa:
            scrape_nykaa(pipeline_dir, nykaa_worklist, nykaa_csv, nykaa_fail_csv, limit=args.limit)
    diff(pipeline_dir, flipkart_csv, d2c_csv, diff_csv)
    if not args.skip_nykaa:
        diff_nykaa(pipeline_dir, nykaa_csv, d2c_csv, nykaa_diff_csv)

    prior_rows = load_prior_rows(data_json_path)
    fk_rows, fk_stats = merge(diff_csv, prior_rows, "flipkart")
    if args.skip_nykaa:
        nk_rows, nk_stats = [], {"new": 0, "carried_open": 0, "frozen_not_an_issue": 0, "reopened": 0, "dropped": 0}
        # Keep whatever Nykaa rows are already in docs/data.json untouched --
        # a Flipkart-only run must not silently wipe out Nykaa's rows.
        nk_rows = [r for r in prior_rows.values() if r.get("platform") == "nykaa"]
    else:
        nk_rows, nk_stats = merge(nykaa_diff_csv, prior_rows, "nykaa")
    rows = fk_rows + nk_rows
    stats_by_platform = {"flipkart": fk_stats, "nykaa": nk_stats}

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
        print(f"Stats: flipkart={fk_stats} | nykaa={nk_stats}")
        print("This confirms the scrape + diff + merge steps work end to end. "
              "Run again WITHOUT --limit for a real, full-catalog refresh.")
        return

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

    write_data_json(rows, data_json_path)
    print(f"Wrote {len(rows)} rows to {data_json_path} ({len(fk_rows)} flipkart, {len(nk_rows)} nykaa)")
    print(f"Stats: flipkart={fk_stats} | nykaa={nk_stats}")

    git_commit_and_push(repo_dir, args.data_path, stats_by_platform)
    print("Done.")


if __name__ == "__main__":
    main()
