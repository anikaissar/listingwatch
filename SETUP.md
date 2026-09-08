# Flipkart QA KAM Dashboard — setup

This repo is the whole pipeline: a weekly scraper that checks Flipkart
listings against nathabit.in, and a live dashboard Daksh and Anika use to
track what needs fixing. Three pieces, in the order you'll set them up:

1. **The dashboard** (`docs/`) — static files, served free by GitHub Pages.
2. **The save service** (`worker/`) — a small Cloudflare Worker that's the
   only thing allowed to write to `docs/data.json`, so clicking a Status
   dropdown actually saves.
3. **The weekly pipeline** (`pipeline/`) — runs on your Windows machine on a
   schedule, re-scrapes both sites, and pushes the refreshed data.

Do them in this order — the pipeline needs the repo and Pages to already
exist, and the dashboard needs the Worker's URL before it's fully useful.

## Part 1 — Create the repo and turn on Pages

1. On github.com, create a new repository (public or private — private
   also works with GitHub Pages on a free personal account, as long as
   you're the owner or it's a paid org).
2. Push everything in this folder to it:
   ```
   cd this-folder
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/YOURNAME/YOURREPO.git
   git push -u origin main
   ```
3. On GitHub: Settings → Pages → Source → "Deploy from a branch" → Branch
   `main`, folder `/docs` → Save.
4. After a minute or two, GitHub shows you the live URL — something like
   `https://yourname.github.io/yourrepo/`. Open it — you'll see the
   dashboard, but Status changes won't save yet (that's Part 2).

## Part 2 — Deploy the save service

Full steps are in `worker/README.md`. Short version: create a GitHub token
scoped to just this repo's contents, create a free Cloudflare account,
`wrangler deploy` the Worker, then paste its URL (and the shared API key
you set) into `docs/index.html`'s `WORKER_URL` / `API_KEY` variables near
the top of the `<script>` block, commit, and push.

Test it by changing a row's Status on the dashboard and checking the
repo's commit history for a new "Status update: ..." commit.

## Part 3 — Set up the weekly scraper

This has to run somewhere that can actually browse flipkart.com and
nathabit.in and reuse a logged-in session — that's your Windows machine,
by design (login stays manual, by you; nothing here ever stores a
password).

1. **Clone this same repo onto your Windows machine** (or link this machine
   to a Claude session to have it set up for you):
   ```
   git clone https://github.com/YOURNAME/YOURREPO.git
   ```
2. **Install requirements** (Python 3.10+, Playwright):
   ```
   pip install -r pipeline/requirements.txt
   playwright install chromium
   ```
3. **Log into Flipkart once**, in the browser this scraper drives, so it has
   a session to reuse:
   ```
   python pipeline/flipkart_pdp_scraper.py --login
   ```
   A browser window opens — log in normally, then close it. This session
   will eventually expire (weeks, not days, typically); when the weekly run
   starts failing to fetch real data, just run this command again.
4. **Test a full run** without waiting a week:
   ```
   python pipeline/weekly_refresh.py --repo-dir /path/to/your/clone --limit 5
   ```
   `--limit 5` scrapes just 5 products so you can see the whole pipeline
   work end to end quickly. Check that it prints row counts and, if
   anything changed, pushes a commit.
5. **Run it for real** (no `--limit`) once the test looks right:
   ```
   python pipeline/weekly_refresh.py --repo-dir /path/to/your/clone
   ```
6. **Schedule it** with Windows Task Scheduler: create a weekly trigger
   (e.g. Thursday mornings) that runs:
   ```
   python C:\path\to\clone\pipeline\weekly_refresh.py --repo-dir C:\path\to\clone
   ```
   Nothing runs unless your machine is on and awake at that time — if you
   miss a week, just run it by hand afterward.

### What "abort" looks like

If a run's scrape looks broken (too few rows flagged, or a sudden huge drop
compared to last time), `weekly_refresh.py` prints an error, exits with a
non-zero code, and pushes nothing — it will not overwrite good data with a
bad scrape, and it will not touch anything Daksh sees. Task Scheduler will
show the task as failed; check the console output (or redirect it to a log
file in the scheduled task's settings) to see why.

There's no automated email alert on failure yet (the earlier version of
this project had one via a Gmail-connected Claude session) — for now,
failures are visible only if you check. Worth revisiting if silent misses
become a problem.

## Ongoing: how a weekly refresh actually updates the dashboard

`weekly_refresh.py` re-scrapes, re-diffs, and merges the fresh results
against whatever `docs/data.json` currently holds — carrying forward every
row's Status and Reminders Sent using the same rules as always (new row →
Open/0; still Open and still flagged → +1 reminder; Not an Issue → frozen
forever; Resolved but still flagged → reopens to Open with a note, +1).
Then it commits and pushes straight to `main`. GitHub Pages redeploys the
updated `docs/data.json` automatically, usually within a minute, and the
dashboard's polling picks it up.

Any Status change Daksh makes on the dashboard between refreshes goes
through the Worker (Part 2) as its own separate commit, so nothing is lost
when the next weekly refresh runs — the refresh always starts from
whatever is currently committed, edits included.
