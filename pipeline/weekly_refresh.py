#!/usr/bin/env python3
"""Weekly refresh for the Flipkart QA KAM dashboard.

Run this on Anika's Windows machine (via Task Scheduler) once a week. It:

  1. Scrapes current Flipkart + nathabit.in listing data (reusing a browser
     session that was already logged in manually -- see "Login" below).
  2. Diffs them (qa_diff.py) and classifies the results into priority tiers
     (same classify() logic build_kam_review.py has always used, imported
     from there so the two never drift apart).
  3. Merges the fresh results against whatever is CURRENTLY in the repo's
     docs/data.json -- same merge-forward rules as before: a brand new row
     starts at Open/0 reminders; a row still Open and still flagged gets
     +1 reminder; a row marked "Not an Issue" freezes its reminder count
     forever; a row marked "Resolved" that's still flagged reopens to Open
     (with a note) and gets +1.
  4. Writes the merged result to docs/data.json, verifies it round-trips
     (loads back and matches what was just written -- the project has been
     burned before by a "successful" write that wasn't), and commits +
     pushes it with git.
  5. Refuses to push if the fresh data looks broken (see --min-rows /
     --max-broken-pct below) -- prints a clear error and exits non-zero
     instead, so nothing bad reaches Daksh.

Login: Flipkart requires you to log in by hand once in the browser this
scraper drives (flipkart_pdp_scraper.py --login), which saves a session
Playwright reuses for unattended runs. That session eventually expires --
when scrapes start failing, run --login again. Nothing in this pipeline
ever asks for or stores a password.

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

from build_kam_review import classify, load_diff_rows  # noqa: E402


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


def diff(pipeline_dir, flipkart_csv, d2c_csv, diff_out):
    run([sys.executable, str(pipeline_dir / "qa_diff.py"),
         "--flipkart", str(flipkart_csv), "--d2c", str(d2c_csv), "--out", str(diff_out)],
        cwd=pipeline_dir)


def load_prior_rows(data_json_path):
    """Returns {(nh_sku, fsn): row_dict} from the current docs/data.json, or {} if it doesn't exist yet."""
    if not data_json_path.exists():
        return {}
    with open(data_json_path, encoding="utf-8") as f:
        payload = json.load(f)
    return {(r["nh_sku"], r["fsn"]): r for r in payload.get("rows", [])}


def merge(diff_csv_path, prior_rows):
    """Same merge-forward rules as build_kam_review.build(), operating on dicts instead of an xlsx sheet."""
    diff_rows = load_diff_rows(str(diff_csv_path))
    classified = []
    stats = {"new": 0, "carried_open": 0, "frozen_not_an_issue": 0, "reopened": 0}

    for rr in diff_rows:
        tier, issue, detail = classify(rr)
        if not tier:
            continue
        key = (rr["nh_sku"], rr["fsn"])
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

        doc_id = f"{rr['nh_sku']}__{rr['fsn']}"
        classified.append({
            "doc_id": doc_id,
            "priority": tier,
            "status": status,
            "nh_sku": rr["nh_sku"],
            "fsn": rr["fsn"],
            "issue": issue,
            "detail": detail,
            "flipkart_title": rr.get("flipkart_title", ""),
            "d2c_title": rr.get("d2c_title", ""),
            "reminders_sent": reminders,
            "status_changed_at": (prior or {}).get("status_changed_at"),
        })

    classified.sort(key=lambda r: (r["priority"], r["nh_sku"]))
    dropped = len(prior_rows) - sum(1 for r in classified if (r["nh_sku"], r["fsn"]) in prior_rows)
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


def git_commit_and_push(repo_dir, data_json_relpath, stats):
    message = (f"Weekly refresh: {stats['new']} new, {stats['carried_open']} carried open, "
               f"{stats['frozen_not_an_issue']} frozen (not an issue), {stats['reopened']} reopened, "
               f"{stats['dropped']} dropped")
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
    ap.add_argument("--min-rows", type=int, default=30, help="abort if fewer than this many rows are flagged")
    ap.add_argument("--max-drop-pct", type=float, default=0.5, help="abort if row count drops by more than this fraction week over week")
    ap.add_argument("--limit", type=int, help="scrape only the first N products (smoke test)")
    ap.add_argument("--skip-scrape", action="store_true", help="reuse existing flipkart/d2c CSVs instead of scraping again (for testing)")
    args = ap.parse_args()

    repo_dir = Path(args.repo_dir).resolve()
    pipeline_dir = Path(args.pipeline_dir).resolve()
    data_json_path = repo_dir / args.data_path

    flipkart_csv = pipeline_dir / "observed_flipkart_latest.csv"
    d2c_csv = pipeline_dir / "nathabit_reference_latest.csv"
    diff_csv = pipeline_dir / "qa_diff_latest.csv"

    if not args.skip_scrape:
        scrape(pipeline_dir, flipkart_csv, d2c_csv, limit=args.limit)
    diff(pipeline_dir, flipkart_csv, d2c_csv, diff_csv)

    prior_rows = load_prior_rows(data_json_path)
    rows, stats = merge(diff_csv, prior_rows)

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
        print(f"Stats: {stats}")
        print("This confirms the scrape + diff + merge steps work end to end. "
              "Run again WITHOUT --limit for a real, full-catalog refresh.")
        return

    problem = sanity_check(rows, prior_rows, args.min_rows, args.max_drop_pct)
    if problem:
        print(f"\nABORTING -- {problem}\nNothing was written or pushed. Check the scrape output before retrying.\n", file=sys.stderr)
        raise SystemExit(1)

    write_data_json(rows, data_json_path)
    print(f"Wrote {len(rows)} rows to {data_json_path}")
    print(f"Stats: {stats}")

    git_commit_and_push(repo_dir, args.data_path, stats)
    print("Done.")


if __name__ == "__main__":
    main()
