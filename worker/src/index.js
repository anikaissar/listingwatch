/**
 * Cloudflare Worker: status-write proxy for the Flipkart QA KAM dashboard.
 *
 * The dashboard (served as static files from GitHub Pages) cannot write to
 * the repo itself — it has no credentials, and shouldn't. This Worker holds
 * the one credential that can (a GitHub token with write access to this repo
 * only) and does exactly one thing: given {doc_id, status}, it updates that
 * row inside docs/data.json and commits the change back to GitHub. GitHub
 * Pages then redeploys the updated file automatically (usually within about
 * a minute), and the dashboard's polling picks it up.
 *
 * Required environment (see ../README.md for how to set these):
 *   GITHUB_TOKEN   (secret) — fine-grained PAT, Contents: Read & Write, scoped to this one repo
 *   GITHUB_OWNER           — e.g. "anika-nathabit"
 *   GITHUB_REPO            — e.g. "flipkart-qa-dashboard"
 *   GITHUB_BRANCH          — e.g. "main"
 *   DATA_PATH              — e.g. "docs/data.json"
 *   API_KEY        (secret, optional) — if set, requests must send header X-Api-Key matching it
 *   ALLOWED_ORIGIN         — e.g. "https://anika-nathabit.github.io" (comma-separated for more than one; "*" allowed but not recommended)
 */

const ALLOWED_STATUSES = ["Open", "Resolved", "Not an Issue"];

function corsHeaders(env, origin) {
  const allowed = (env.ALLOWED_ORIGIN || "*").split(",").map((s) => s.trim());
  const allowOrigin = allowed.includes("*") ? "*" : (allowed.includes(origin) ? origin : allowed[0]);
  return {
    "Access-Control-Allow-Origin": allowOrigin || "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Api-Key",
  };
}

function jsonResponse(body, status, extraHeaders) {
  return new Response(JSON.stringify(body), {
    status: status || 200,
    headers: Object.assign({ "Content-Type": "application/json" }, extraHeaders || {}),
  });
}

// Workers' btoa() only handles Latin-1; this encodes a UTF-8 string to base64 safely.
function utf8ToBase64(str) {
  const bytes = new TextEncoder().encode(str);
  let binary = "";
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function base64ToUtf8(b64) {
  const binary = atob(b64.replace(/\n/g, ""));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}

async function githubGetFile(env) {
  const url = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/contents/${env.DATA_PATH}?ref=${env.GITHUB_BRANCH}`;
  const res = await fetch(url, {
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "kam-status-writer-worker",
    },
  });
  if (!res.ok) {
    throw new Error(`GitHub GET failed: ${res.status} ${await res.text()}`);
  }
  return res.json(); // { content, sha, ... }
}

async function githubPutFile(env, newContentStr, sha, message) {
  const url = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/contents/${env.DATA_PATH}`;
  const res = await fetch(url, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "kam-status-writer-worker",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      message,
      content: utf8ToBase64(newContentStr),
      sha,
      branch: env.GITHUB_BRANCH,
    }),
  });
  return res;
}

async function updateStatus(env, docId, status) {
  // Retry once on a 409 (someone else's write landed between our GET and PUT).
  for (let attempt = 0; attempt < 2; attempt++) {
    const file = await githubGetFile(env);
    const text = base64ToUtf8(file.content);
    const data = JSON.parse(text);
    const row = (data.rows || []).find((r) => r.doc_id === docId);
    if (!row) {
      return { ok: false, code: 404, error: `No row with doc_id "${docId}"` };
    }
    const changedAt = new Date().toISOString();
    row.status = status;
    row.status_changed_at = changedAt;
    data.generated_at = data.generated_at || changedAt; // don't touch the pipeline's own timestamp field meaning; see README

    const newText = JSON.stringify(data, null, 2);
    const putRes = await githubPutFile(env, newText, file.sha, `Status update: ${docId} -> ${status}`);
    if (putRes.ok) {
      return { ok: true, status_changed_at: changedAt };
    }
    if (putRes.status === 409 && attempt === 0) {
      continue; // retry with a fresh sha
    }
    return { ok: false, code: putRes.status, error: await putRes.text() };
  }
  return { ok: false, code: 409, error: "Conflict persisted after retry" };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = request.headers.get("Origin") || "";
    const cors = corsHeaders(env, origin);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }

    if (url.pathname === "/health") {
      return jsonResponse({ ok: true }, 200, cors);
    }

    if (url.pathname === "/status" && request.method === "POST") {
      if (env.API_KEY) {
        const key = request.headers.get("X-Api-Key");
        if (key !== env.API_KEY) {
          return jsonResponse({ ok: false, error: "Unauthorized" }, 401, cors);
        }
      }
      let body;
      try {
        body = await request.json();
      } catch (e) {
        return jsonResponse({ ok: false, error: "Invalid JSON body" }, 400, cors);
      }
      const { doc_id, status } = body || {};
      if (!doc_id || typeof doc_id !== "string") {
        return jsonResponse({ ok: false, error: "doc_id is required" }, 400, cors);
      }
      if (!ALLOWED_STATUSES.includes(status)) {
        return jsonResponse({ ok: false, error: `status must be one of ${ALLOWED_STATUSES.join(", ")}` }, 400, cors);
      }
      try {
        const result = await updateStatus(env, doc_id, status);
        if (!result.ok) {
          return jsonResponse(result, result.code || 500, cors);
        }
        return jsonResponse(result, 200, cors);
      } catch (err) {
        return jsonResponse({ ok: false, error: String(err && err.message || err) }, 500, cors);
      }
    }

    return jsonResponse({ ok: false, error: "Not found" }, 404, cors);
  },
};
