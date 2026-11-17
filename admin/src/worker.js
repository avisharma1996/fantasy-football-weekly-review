// Admin backend for the power rankings site.
//
// Serves the admin page and a small JSON API that reads and commits week files through the GitHub API, so rankings
// can be written and published from a phone. Cloudflare Access sits in front of the whole hostname and handles the
// login; this Worker also verifies the Access token itself, so a missing or misconfigured Access policy fails closed.
//
// Secrets (wrangler secret put): GITHUB_TOKEN
// Vars (wrangler.toml):          GITHUB_REPO, GITHUB_BRANCH, ACCESS_TEAM_DOMAIN, ACCESS_AUD, SITE_URL, ALLOWED_EMAILS

import PAGE from "./admin.html";

const WEEK_FILE = /^weeks\/\d{4}\/(drafts\/)?week-\d{2}\.txt$/;
const DRAFT_FILE = /^weeks\/\d{4}\/drafts\/week-\d{2}\.txt$/;
const MEDIA_FILE = /^media\/[a-z0-9][a-z0-9-]{0,60}\/[a-z0-9][a-z0-9._-]{0,100}\.(png|jpe?g|gif|webp|mp4|webm|mov)$/;
const MAX_UPLOAD = 70 * 1024 * 1024; // base64 request body; about 50 MB of actual file

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const user = await signedIn(request, env, url);
  if (!user) return text("Not signed in. This page must sit behind Cloudflare Access (see admin/README.md).", 403);

    if (url.pathname === "/" || url.pathname === "/index.html") {
      return new Response(PAGE, {
        headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "x-frame-options": "DENY" },
      });
    }
    if (!url.pathname.startsWith("/api/")) return text("Not found", 404);
    // A custom header can't be sent cross-site without a CORS preflight, which this Worker never grants.
    if (request.method !== "GET" && request.headers.get("x-admin") !== "1") return text("Missing x-admin header", 400);

    try {
      const route = `${request.method} ${url.pathname}`;
      if (route === "GET /api/state") return json(await state(env, url.searchParams.get("season"), user));
      if (route === "GET /api/week") return json(await week(env, url.searchParams));
      if (route === "GET /api/runs") return json(await runs(env));
      if (route === "POST /api/new-week") return json(await newWeek(env, await request.json()));
      if (route === "POST /api/blob") return await blob(env, request);
      if (route === "POST /api/commit") return json(await commit(env, await request.json()));
      return text("Not found", 404);
    } catch (e) {
      return json({ error: e.message }, e.status || 500);
    }
  },
};

// ---------------------------------------------------------------- auth

let certCache = { at: 0, keys: [] };

async function signedIn(request, env, url) {
  // Local testing only (wrangler dev --var DEV_NO_AUTH:1). Requests through Cloudflare never come from loopback.
  if (env.DEV_NO_AUTH === "1" && ["127.0.0.1", "::1"].includes(request.headers.get("cf-connecting-ip"))) return "dev@localhost";
  const token = request.headers.get("cf-access-jwt-assertion");
  if (!token || !env.ACCESS_TEAM_DOMAIN || !env.ACCESS_AUD) return null;
  try {
    const [h, p, s] = token.split(".");
    const header = JSON.parse(b64urlText(h));
    const claims = JSON.parse(b64urlText(p));
    const team = env.ACCESS_TEAM_DOMAIN.replace(/\/$/, "");
    if (Date.now() - certCache.at > 3600e3 || !certCache.keys.some((k) => k.kid === header.kid)) {
      const r = await fetch(`${team}/cdn-cgi/access/certs`);
      certCache = { at: Date.now(), keys: (await r.json()).keys || [] };
    }
    const jwk = certCache.keys.find((k) => k.kid === header.kid);
    if (!jwk) return null;
    const algo = { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" };
    const key = await crypto.subtle.importKey("jwk", jwk, algo, false, ["verify"]);
    const ok = await crypto.subtle.verify(algo, key, b64urlBytes(s), new TextEncoder().encode(`${h}.${p}`));
    const now = Date.now() / 1000;
    const aud = [].concat(claims.aud);
    if (!ok || claims.exp < now || claims.iss !== team || !aud.includes(env.ACCESS_AUD)) return null;
    const email = (claims.email || "").toLowerCase();
    const allowed = (env.ALLOWED_EMAILS || "").toLowerCase().split(/[\s,]+/).filter(Boolean);
    if (allowed.length && !allowed.includes(email)) return null;
    return email || "signed in";
  } catch {
    return null;
  }
}

function b64urlBytes(s) {
  const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

function b64urlText(s) {
  return new TextDecoder().decode(b64urlBytes(s));
}

// ---------------------------------------------------------------- GitHub

async function gh(env, path, init = {}) {
  const r = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}${path}`, {
    ...init,
    headers: {
      authorization: `Bearer ${env.GITHUB_TOKEN}`,
      accept: "application/vnd.github+json",
      "user-agent": "redvines-admin",
      "x-github-api-version": "2022-11-28",
      ...(init.headers || {}),
    },
  });
  if (r.status === 404 && init.allow404) return null;
  if (!r.ok) {
    const body = await r.text();
    throw httpError(r.status === 409 || r.status === 422 ? r.status : 502, `GitHub ${r.status} on ${path}: ${body.slice(0, 300)}`);
  }
  return r.status === 204 ? {} : r.json();
}

const branch = (env) => env.GITHUB_BRANCH || "main";

/** {text, sha} for a file on the branch, or null when it doesn't exist. */
async function readFile(env, path) {
  const f = await gh(env, `/contents/${path}?ref=${branch(env)}`, { allow404: true });
  if (!f || Array.isArray(f)) return null;
  const bytes = Uint8Array.from(atob(f.content.replace(/\n/g, "")), (c) => c.charCodeAt(0));
  return { text: new TextDecoder().decode(bytes), sha: f.sha };
}

async function listDir(env, path) {
  const items = await gh(env, `/contents/${path}?ref=${branch(env)}`, { allow404: true });
  return Array.isArray(items) ? items : [];
}

// ---------------------------------------------------------------- API

async function state(env, season, user) {
  const config = JSON.parse((await readFile(env, "config.json")).text);
  const seasons = Object.keys(config.seasons).sort();
  season = seasons.includes(season) ? season : seasons[seasons.length - 1];
  const [files, drafts, media] = await Promise.all([
    listDir(env, `weeks/${season}`),
    listDir(env, `weeks/${season}/drafts`),
    listDir(env, "media"),
  ]);
  const published = weekNums(files);
  return {
    user,
    seasons,
    season,
    published,
    drafts: weekNums(drafts),
    startable: await startable(season, published),
    mediaFolders: media.filter((f) => f.type === "dir").map((f) => f.name),
    siteUrl: env.SITE_URL || "",
    repo: env.GITHUB_REPO,
    runs: await runs(env),
  };
}

async function week(env, params) {
  const season = params.get("season") || "";
  const n = +params.get("week");
  if (!/^\d{4}$/.test(season) || !(n >= 1 && n <= 25)) throw httpError(400, "Bad season or week");
  const nn = String(n).padStart(2, "0");
  const paths = { published: `weeks/${season}/week-${nn}.txt`, draft: `weeks/${season}/drafts/week-${nn}.txt` };
  const [published, draft, data] = await Promise.all([
    readFile(env, paths.published),
    readFile(env, paths.draft),
    readFile(env, `weeks/${season}/week-${nn}.json`),
  ]);
  if (!published && !draft) throw httpError(404, `Week ${n} hasn't been started yet`);
  // Only what the editor shows; the full week data is much bigger.
  const teams = {};
  if (data) {
    for (const t of Object.values(JSON.parse(data.text).teams)) {
      teams[t.name.trim().toLowerCase()] = { avatar: t.avatar || null, manager: t.manager || "" };
    }
  }
  return {
    season,
    week: n,
    paths,
    published: published && { sha: published.sha, text: published.text },
    draft: draft && { sha: draft.sha, text: draft.text },
    teams,
  };
}

async function runs(env) {
  const r = await gh(env, `/actions/runs?per_page=15&branch=${branch(env)}`);
  // GitHub's own Pages deploys show up here too; only list this repo's workflows.
  return r.workflow_runs.filter((w) => w.event !== "dynamic").map((w) => ({
    id: w.id,
    name: w.name,
    title: w.display_title,
    event: w.event,
    status: w.status,
    conclusion: w.conclusion,
    created: w.created_at,
    url: w.html_url,
  }));
}

const weekNums = (items) => items.map((f) => f.name.match(/^week-(\d{2})\.txt$/)).filter(Boolean).map((m) => +m[1]);

/**
 * The one week that can be started: the most recent NFL week whose games are all done, as long as it isn't
 * published yet. Sleeper's `week` moves to the next week once the last game of the previous one ends.
 * Returns {week, reason}; week is null when nothing can be started right now.
 */
async function startable(season, published) {
  const r = await fetch("https://api.sleeper.app/v1/state/nfl", { headers: { "user-agent": "redvines-admin" } });
  if (!r.ok) return { week: null, reason: "Couldn't reach Sleeper to check the current week. Try again in a minute." };
  const nfl = await r.json();
  if (String(nfl.league_season || nfl.season) !== season) {
    return { week: null, reason: `Only the current season (${nfl.league_season || nfl.season}) can have weeks started.` };
  }
  const last = nfl.season_type === "regular" || nfl.season_type === "post" ? nfl.week - 1 : 0;
  if (last < 1) return { week: null, reason: "No NFL week has finished yet this season." };
  if (published.includes(last)) {
    return { week: null, reason: `Week ${last} is already published. Week ${last + 1} opens once its games are over.` };
  }
  return { week: last, reason: `Week ${last} is done. Pull its stats to start ranking.` };
}

async function newWeek(env, body) {
  const week = +body.week;
  const season = String(body.season || "");
  if (!(week >= 1 && week <= 25) || !/^\d{4}$/.test(season)) throw httpError(400, "Bad season or week");
  const allowed = await startable(season, weekNums(await listDir(env, `weeks/${season}`)));
  if (allowed.week !== week) throw httpError(400, allowed.week ? `Only week ${allowed.week} can be started right now.` : allowed.reason);
  await gh(env, "/actions/workflows/new-week.yml/dispatches", {
    method: "POST",
    body: JSON.stringify({ ref: branch(env), inputs: { week: String(week), season } }),
  });
  return { ok: true };
}

// The browser sends GitHub's blob body ({"encoding":"base64","content":...}) and it is streamed straight through,
// so large videos never have to be parsed or re-encoded here.
async function blob(env, request) {
  const size = +request.headers.get("content-length");
  if (!size) return json({ error: "Missing content-length" }, 411);
  if (size > MAX_UPLOAD) return json({ error: "File too big (about 50 MB max). Compress it first." }, 413);
  const r = await gh(env, "/git/blobs", {
    method: "POST",
    body: request.body,
    headers: { "content-type": "application/json", "content-length": String(size) },
  });
  return json({ sha: r.sha });
}

/**
 * One commit on the branch.
 * files:   [{path, content (week text) | sha (uploaded media blob), base}]. When `base` is given (a blob sha, or null
 *          for "must not exist yet"), the commit is refused if the file changed since the editor loaded it.
 * deletes: [draft paths]
 */
async function commit(env, body) {
  const files = body.files || [];
  const deletes = body.deletes || [];
  const message = String(body.message || "Update rankings").slice(0, 200);
  if (!files.length && !deletes.length) throw httpError(400, "Nothing to commit");
  for (const f of files) {
    const ok = WEEK_FILE.test(f.path) ? typeof f.content === "string" && f.content.length < 200000
      : MEDIA_FILE.test(f.path) ? /^[0-9a-f]{40}$/.test(f.sha || "") : false;
    if (!ok) throw httpError(400, `Not allowed: ${f.path}`);
  }
  for (const p of deletes) if (!DRAFT_FILE.test(p)) throw httpError(400, `Not allowed to delete: ${p}`);

  const current = {};
  await Promise.all([...files.map((f) => f.path), ...deletes].map(async (p) => {
    const f = await gh(env, `/contents/${p}?ref=${branch(env)}`, { allow404: true });
    current[p] = f && !Array.isArray(f) ? f.sha : null;
  }));
  for (const f of files) {
    if ("base" in f && current[f.path] !== f.base) {
      throw httpError(409, f.base === null ? `${f.path} already exists`
        : `${f.path} was changed somewhere else since you opened it. Reload to get the latest.`);
    }
  }

  const tree = [
    ...files.map((f) => ({ path: f.path, mode: "100644", type: "blob", ...(f.sha ? { sha: f.sha } : { content: f.content }) })),
    ...deletes.filter((p) => current[p]).map((p) => ({ path: p, mode: "100644", type: "blob", sha: null })),
  ];
  // Retry if something else (a workflow, a local push) moved the branch between reading and updating it.
  for (let attempt = 0; ; attempt++) {
    const ref = await gh(env, `/git/ref/heads/${branch(env)}`);
    const head = await gh(env, `/git/commits/${ref.object.sha}`);
    const newTree = await gh(env, "/git/trees", { method: "POST", body: JSON.stringify({ base_tree: head.tree.sha, tree }) });
    const c = await gh(env, "/git/commits", {
      method: "POST",
      body: JSON.stringify({ message, tree: newTree.sha, parents: [ref.object.sha] }),
    });
    try {
      await gh(env, `/git/refs/heads/${branch(env)}`, { method: "PATCH", body: JSON.stringify({ sha: c.sha, force: false }) });
    } catch (e) {
      if (e.status === 422 && attempt < 2) continue;
      throw e;
    }
    const shas = {};
    for (const f of files) shas[f.path] = f.sha || (await blobSha(f.content));
    return { ok: true, commit: c.sha, shas };
  }
}

// ---------------------------------------------------------------- helpers

/** Git's id for a file's contents, so the editor knows the new version without another API call. */
async function blobSha(content) {
  const body = new TextEncoder().encode(content);
  const head = new TextEncoder().encode(`blob ${body.length}\0`);
  const all = new Uint8Array(head.length + body.length);
  all.set(head);
  all.set(body, head.length);
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-1", all));
  return Array.from(hash, (b) => b.toString(16).padStart(2, "0")).join("");
}

function httpError(status, message) {
  return Object.assign(new Error(message), { status });
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });
}

function text(body, status = 200) {
  return new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8" } });
}
