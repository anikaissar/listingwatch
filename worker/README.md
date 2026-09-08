# Status-write Worker — deploy steps

This is the one piece of "backend" the dashboard needs. GitHub Pages can only
serve static files, so this small Cloudflare Worker is what actually saves a
Status click: the dashboard calls it, it commits the change to
`docs/data.json` in your GitHub repo using a token only it holds, and GitHub
Pages redeploys the updated file automatically.

Free tier is enough for this (Cloudflare Workers' free plan covers far more
requests per day than a few people clicking dropdowns).

## 1. Create a GitHub token the Worker can use

GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token.

- Resource owner: your account (or the org, if the repo lives there)
- Repository access: **Only select repositories** → pick this one repo
- Permissions: **Contents → Read and write** (nothing else needed)
- Copy the token now — you can't see it again after leaving the page.

## 2. Install Wrangler (Cloudflare's CLI) and log in

```
npm install -g wrangler
wrangler login
```

This opens a browser to connect Wrangler to your (free) Cloudflare account —
create one at cloudflare.com if you don't have one yet.

## 3. Edit `wrangler.toml`

Fill in the four `REPLACE_WITH_...` values at the top:
- `GITHUB_OWNER` — your GitHub username (or org name)
- `GITHUB_REPO` — the repo you created for this dashboard
- `ALLOWED_ORIGIN` — your GitHub Pages URL, e.g. `https://yourname.github.io`
  (this restricts which sites are allowed to call the Worker — leave it
  pointed at your real Pages URL, not `*`, once you know it)

## 4. Set the secrets (never go in wrangler.toml or the repo)

```
cd worker
wrangler secret put GITHUB_TOKEN
# paste the token from step 1 when prompted

wrangler secret put API_KEY
# make up any random string — this is a shared password between the
# dashboard and the Worker so random people on the internet can't spam
# status changes into your repo. Save it, you'll need to paste the same
# value into docs/index.html's API_KEY setting.
```

## 5. Deploy

```
wrangler deploy
```

This prints the Worker's URL, something like:
`https://kam-status-writer.yourname.workers.dev`

## 6. Wire the dashboard to it

In `docs/index.html`, set:
```js
var WORKER_URL = "https://kam-status-writer.yourname.workers.dev";
var API_KEY = "the same random string you set in step 4";
```
Commit and push — GitHub Pages will pick it up.

## Test it

```
curl https://kam-status-writer.yourname.workers.dev/health
# {"ok":true}
```

Then open the dashboard, change a row's Status, and check the repo's commit
history on GitHub — you should see a new commit like
"Status update: BA-SHO-BM-110__BBCGGBWPXNH9M2NN -> Resolved" within a few
seconds, and the dashboard update on the page immediately (other viewers
pick it up within ~20-60 seconds as they poll and GitHub Pages redeploys).

## A note on security

This Worker is reachable by anyone who knows its URL — the `API_KEY` above
is the only thing stopping a stranger from POSTing status changes into your
repo. That's an appropriate level of protection for an internal QA tracker
like this one (nothing sensitive is stored), but don't reuse this pattern
for anything that needs real access control. If you want to tighten it
further later, Cloudflare Access (Zero Trust) can restrict the Worker to
signed-in nathabit.in accounts only — ask if you want that set up.
