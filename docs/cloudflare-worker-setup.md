# Cloudflare Worker + KV setup

This guide wires up edge-side API key authentication for carbon-proxy. The
dashboard issues tokens, a Cloudflare Worker validates them at the edge against
Workers KV, and the origin (behind a Cloudflare Tunnel) sees only requests that
already carry a `X-Carbon-Source` label.

## Architecture

```
                           ┌─────────────────────────┐
LLM client ──[Bearer]────► │  Cloudflare             │
                           │  ┌───────────────────┐  │
                           │  │ Worker            │──┼──► KV (sha256(token) → label)
                           │  │  - hash bearer    │  │           ▲
                           │  │  - lookup in KV   │  │           │ writes
                           │  │  - strip Auth     │  │           │ (CF REST API)
                           │  │  - set            │  │           │
                           │  │    X-Carbon-Source│  │           │
                           │  └─────────┬─────────┘  │           │
                           └────────────┼────────────┘           │
                                        │ tunnel                 │
                                        ▼                        │
                           ┌─────────────────────────┐           │
Browser ──[Access SSO]───► │  carbon-proxy (origin)  │───────────┘
                           │  - dashboard            │   reconciler loop +
                           │  - /carbon/api_keys     │   on-create / on-revoke
                           │  - logs requests        │   pushes from origin
                           └─────────────────────────┘
```

**Source of truth.** The origin's SQLite `api_keys` table is authoritative. KV
is a projection used by the worker. On token create the origin pushes
`(sha256(token), label)` to KV; on revoke it deletes the key. A reconciler
loop (default every 5 min) heals drift if a CF API call ever fails.

**What the worker sees vs. what the origin sees.** The worker reads the bearer
header, hashes it, looks it up in KV, then strips `Authorization` before
forwarding. The origin never receives the bearer; instead `identify()` reads
the `X-Carbon-Source` header the worker set, and the request is logged with
that label.

## Auth model (what protects what)

There are three categories of route at the origin. They have **different**
auth mechanisms, and conflating them is the easiest way to leave a gap.

| Route | Auth at origin | Worker adds auth? | CF Access adds auth? |
|---|---|---|---|
| `POST /v1/...` (proxy catch-all) | **`X-Worker-Origin-Secret` header**, checked only when `CF-Connecting-IP` is present (cloudflared sets it on every forwarded request) | Yes (bearer ↔ KV) | n/a (worker fronts this) |
| `GET /dashboard`, `GET /carbon/*` reporting | **none** | n/a (not fronted) | **Yes, only if you set it up** |
| `POST/PATCH/DELETE /carbon/api_keys/*`, `POST /carbon/offset`, `POST /carbon/auto_offset/toggle` | `OFFSET_API_KEY` bearer, always | n/a | Yes, if set up |

Three things follow from this:

1. **The proxy catch-all is gated by a shared secret with the worker, for
   Cloudflare-fronted traffic only.** The worker stamps every forwarded
   request with `X-Worker-Origin-Secret`; the origin rejects any
   Cloudflare-fronted catch-all request missing or mismatching it. This
   closes the "direct curl to the tunnel hostname for free inference"
   bypass. The check is gated on the `CF-Connecting-IP` header (cloudflared
   sets it on every forwarded request) so internal Docker callers — e.g. an
   OpenWebUI container pointed at `http://carbon-proxy:8080/v1` — are not
   blocked. If `WORKER_ORIGIN_SECRET` is left empty (e.g. local dev), the
   check is skipped entirely.
2. **The dashboard is open by default.** Without Cloudflare Access in
   front, anyone who can reach the dashboard hostname can read all labels,
   request logs, and source breakdowns. They can't *mutate* anything
   without the `OFFSET_API_KEY` bearer, but the information disclosure is
   real. **You must set up Access (step 7) for the dashboard to be private.**
3. **Direct-origin hits via the catch-all need the worker secret.** Even
   with a valid bearer, you can't bypass the worker by curling the tunnel
   hostname directly — the catch-all rejects without the
   `X-Worker-Origin-Secret` header. `identify()` still does the bearer →
   label lookup for the requests that *do* pass, so logs stay correctly
   labeled.

## Prerequisites

- A Cloudflare account
- A domain on Cloudflare (e.g. `yourdomain.com`)
- The carbon-proxy already running behind a Cloudflare Tunnel (origin not
  directly internet-exposed)
- `wrangler` installed locally (`npm i -g wrangler` or `brew install
  cloudflare-wrangler`)

> **If you cloned this repo**, the worker source already exists at
> `carbon-proxy/worker/`. Before deploying, bootstrap your local config:
>
> ```bash
> cd carbon-proxy/worker
> cp wrangler.example.jsonc wrangler.jsonc   # real config is gitignored
> npm install                                # installs wrangler from package.json
> npx wrangler login
> ```
>
> Then fill in the `PUBLIC_HOST`, `ORIGIN_HOST`, `routes.pattern`, and
> `kv_namespaces[0].id` placeholders in `wrangler.jsonc` as you walk through
> the steps below.

## 1. Create a KV namespace

In the Cloudflare dashboard:

1. **Workers & Pages** → **KV** → **Create a namespace**
2. Name it `carbon-proxy-api-keys`
3. Copy the **Namespace ID** (looks like `a1b2c3...`) — you'll use it twice:
   once in the origin `.env` and once in `wrangler.jsonc`

## 2. Create an API token for the origin

The origin needs write access to that one KV namespace. Use least-privilege:

1. **My Profile** → **API Tokens** → **Create Token** → **Custom token**
2. **Permissions**: `Account` → `Workers KV Storage` → `Edit`
3. **Account Resources**: `Include` → your account
4. (Optional but recommended) **Zone Resources**: not needed for KV
5. Create and copy the token immediately — it is shown once

You also need your **Account ID**: it's in the right sidebar of any zone
overview page in the CF dashboard.

## 3. Configure the origin

Add to your `.env`:

```bash
CLOUDFLARE_API_TOKEN=<token from step 2>
CLOUDFLARE_ACCOUNT_ID=<account id>
KV_NAMESPACE_ID=<namespace id from step 1>

# Already required for /carbon/offset and now also for /carbon/api_keys
OFFSET_API_KEY=<any strong random string>

# Shared secret with the worker. Generate once, then paste the same value
# into the worker via `wrangler secret put ORIGIN_SECRET`. Blocks direct-
# origin bypass of the worker's bearer check (see auth-model section).
WORKER_ORIGIN_SECRET=<openssl rand -hex 32>
```

Optional:

```bash
# How often the reconciler heals KV drift (default 300)
KV_RECONCILER_INTERVAL_S=300
```

Restart the carbon-proxy container. You should see this in the logs:

```
[carbon-proxy.api_keys] INFO: KV reconciler started (every 300s)
```

If KV isn't configured the reconciler logs `KV not configured -- reconciler
disabled.` and the `POST /carbon/api_keys` endpoint returns 503.

## 4. The worker source

The worker ships at `carbon-proxy/worker/src/worker.js`. The full file is
~50 lines; the shape of it:

```javascript
export default {
  async fetch(request, env) {
    // 1. Pull bearer from Authorization header → 401 if missing
    // 2. sha256(bearer) → KV lookup → 401 if no hit
    // 3. Strip Authorization, set X-Carbon-Source: <label>
    // 4. If env.ORIGIN_SECRET set, set X-Worker-Origin-Secret
    // 5. Rewrite hostname (PUBLIC_HOST → ORIGIN_HOST) and forward
  },
};
```

Read `worker/src/worker.js` for the actual implementation. Things worth
noting about the design:

- **KV stores `sha256(token) → label` — never plaintext.** Defense in depth
  against a KV dump or compromised CF account: an attacker who reads KV
  still can't replay the tokens. Origin (`db.api_keys.token_hash`) also
  only stores the hash; plaintext exists in memory for one request during
  creation, then in the user's clipboard.
- **`cacheTtl: 60`** is the per-edge KV read cache. Lower = faster
  revocation across the global edge at the cost of more KV reads. 60s is
  the floor.
- **`ORIGIN_SECRET`** is a wrangler **secret** (not in `vars`), set via
  `wrangler secret put ORIGIN_SECRET`. The same value goes in the origin's
  `.env` as `WORKER_ORIGIN_SECRET`. The `if (env.ORIGIN_SECRET)` guard
  keeps the worker functional before you provision the secret.
- **Hostname rewrite** is the only place the worker encodes your topology.
  Both hostnames live in `wrangler.jsonc` `vars` so the worker file stays
  generic.

## 5. Configure wrangler

The repo ships `wrangler.example.jsonc` with the right shape. After
`cp wrangler.example.jsonc wrangler.jsonc` (step 0), fill in:

```jsonc
{
  "name": "carbon-proxy-auth",
  "main": "src/worker.js",
  "compatibility_date": "2026-05-30",

  "observability": { "enabled": true, "head_sampling_rate": 1 },

  "vars": {
    "PUBLIC_HOST": "api.yourdomain.com",       // where clients connect
    "ORIGIN_HOST": "carbon.yourdomain.com"     // tunnel exit → carbon-proxy
  },

  "kv_namespaces": [
    { "binding": "API_KEYS", "id": "<KV_NAMESPACE_ID>" }  // from step 1
  ],

  "routes": [
    { "pattern": "api.yourdomain.com", "custom_domain": true }
  ]
}
```

The `routes[].custom_domain: true` form is the easiest path — Cloudflare
auto-creates the DNS record and provisions a TLS cert on first deploy.
The hostname must be on a Cloudflare-managed zone.

Deploy:

```bash
cd carbon-proxy/worker
npx wrangler secret put ORIGIN_SECRET   # paste the WORKER_ORIGIN_SECRET value
npx wrangler deploy
```

## 6. Route traffic through the worker

The `routes` block in `wrangler.jsonc` (step 5) handles this declaratively.
Two reasonable shapes:

### Option A: Custom domain on a dedicated public hostname (recommended)

```jsonc
"routes": [
  { "pattern": "api.yourdomain.com", "custom_domain": true }
]
```

Clients talk to `api.yourdomain.com` (worker); the worker forwards to
`carbon.yourdomain.com` (tunnel exit). This is the setup the example
config ships with. Distinct hostnames keep the LLM API and the tunneled
dashboard easy to reason about, and CF Access on `carbon.*` doesn't
interfere with API traffic.

### Option B: Route on the same hostname, path-scoped

If you'd rather have a single hostname:

```jsonc
"routes": [
  { "pattern": "api.yourdomain.com/v1/*", "zone_name": "yourdomain.com" }
]
```

Worker intercepts only `/v1/*`; the dashboard and `/carbon/*` reach origin
directly via tunnel. Worker code is identical either way.

## 6.5. Whitelist the API for OpenAI-style SDK clients (WAF skip rule)

Cloudflare's WAF Managed Rules pattern-match on User-Agent strings like
`OpenAI/Python <version>` and return a 403 "Your request was blocked"
page before the request reaches the worker. Without an exception, every
caller using the official OpenAI Python or JS SDK will be edge-blocked
even with a valid bearer.

Bearer-vs-KV auth at the worker is already the meaningful gate for
`/v1/*`, so it's safe to skip the managed checks on that path.

**Add a custom WAF rule** in **Security → Security rules → Custom rules**:

- **Name**: `Skip WAF for /v1/* API clients`
- **Expression**:
  ```
  (http.host eq "api.yourdomain.com" and starts_with(http.request.uri.path, "/v1/"))
  ```
- **Action**: Skip
  - All managed rules
  - All Super Bot Fight Mode rules (if on Pro+)
  - Browser Integrity Check (under "Skip products")
- **Place at**: top of the list

This is path-scoped to `/v1/*` only — `carbon.yourdomain.com/dashboard`
and `/carbon/*` still get full WAF + Bot protection, gated by CF Access.

Bot Fight Mode (Free plan) **cannot** be skipped by custom rules. If
you're on Free and Bot Fight Mode is enabled, you'll need to disable it
zone-wide; the worker's bearer check is doing the real gating anyway.

Verify after deploy with a Stainless UA — should reach the worker and
401 on a fake token (instead of 403'ing at the edge):

```bash
curl -sS -D - -o /dev/null -X POST https://api.yourdomain.com/v1/chat/completions \
  -H "Authorization: Bearer fake" \
  -H "Content-Type: application/json" \
  -H "User-Agent: OpenAI/Python 1.50.0" \
  -d '{"model":"x","messages":[{"role":"user","content":"hi"}]}' \
  | grep -iE "^HTTP|^content-type"
# expected: HTTP/2 401  +  content-type: application/json
```

## 7. Lock down the dashboard with Cloudflare Access

The dashboard (`/dashboard`, `/carbon/*`) must not be publicly reachable —
Access is the simplest way to gate it.

1. **Zero Trust dashboard** (one.dash.cloudflare.com) → **Access** →
   **Applications** → **Add an application** → **Self-hosted**
2. **Application domain**: whatever hostname routes through the tunnel to
   your origin's dashboard (e.g. `carbon.yourdomain.com`)
3. **Application path**: leave blank (protects the whole hostname) or
   restrict to `/dashboard*` and `/carbon/*`
4. **Identity providers**: enable at least one — Google, GitHub, or
   one-time PIN to email all work
5. **Policy**: `Include` → `Emails` → your address
6. Save

After this, hitting the dashboard hostname triggers an SSO challenge; on
success, CF sets a session cookie and the request proxies to origin.

**Belt-and-suspenders (optional).** The origin can verify the
`Cf-Access-Jwt-Assertion` header CF attaches on Access-authenticated
requests. This protects you if the tunnel ever gets misconfigured. Not
required for v1.

## 8. End-to-end test

1. Browser → `https://carbon.yourdomain.com/dashboard` → Access challenge → sign in
2. Scroll to **API Keys** → **New Key** → label `claude-code` → **Create**
3. Modal shows the token `ckp_…` exactly once. Copy it.
4. From a terminal:

   ```bash
   curl https://api.yourdomain.com/v1/models \
     -H "Authorization: Bearer ckp_..."
   ```

5. In the dashboard, **Recent Requests** should show a new row with
   `source = claude-code`.

Revoke flow:

1. Dashboard → **API Keys** → row → **Revoke** → confirm
2. Same `curl` returns `401 Unauthorized` within ~60 seconds
   (the worker's KV read cache TTL)

## Operational notes

### Revocation timing

KV is eventually consistent with up to ~60 second propagation across edges
and per-edge read caches. So a revoked token may continue to be accepted by
the worker for up to ~60 seconds at distant edges even after the origin
marks it revoked.

Direct-origin hits via the catch-all are blocked entirely (no
`X-Worker-Origin-Secret`), so the lag doesn't open a bypass — it only
delays revocation through the worker path. If you need hard sub-second
revocation across edges, replace KV with **Durable Objects** for
strongly-consistent reads at the cost of ~50ms instead of ~10ms cached.
Almost no self-hosted setup needs this.

### Drift and the reconciler

The origin pushes to KV synchronously on create/revoke. If a CF API call
ever fails:

- **Create** rolls back the local row and returns 502 — you don't get a
  token that would 401.
- **Revoke** leaves the local row marked revoked but logs a warning. The
  reconciler picks up the orphan KV entry on its next tick (default 5 min)
  and deletes it.

The reconciler PUTs every active key on each tick (idempotent — this is
how stale labels from a failed PATCH get healed in addition to plain
missing/orphan drift). Each tick result looks like:

```
Reconciler healed drift: {'active_local': 3, 'kv_keys': 4, 'pushed_to_kv': 3, 'added_to_kv': 0, 'removed_from_kv': 1}
```

- `pushed_to_kv` — keys re-PUT this tick (always equals `active_local` on success)
- `added_to_kv` — subset that wasn't in KV at all (drift from a failed POST)
- `removed_from_kv` — orphan keys deleted (revoked locally, never deleted from KV)

This log line only emits when `added_to_kv` or `removed_from_kv` is non-zero
— a healthy steady state is silent.

### Token format

Tokens are `ckp_` + 32 url-safe random bytes (~43 chars). The prefix is
recognizable to secret scanners and makes leaked tokens obvious in logs.
Only the SHA-256 hash is persisted anywhere — neither the origin nor KV
holds plaintext.

### Token rotation

There's no built-in rotation flow. To rotate:

1. Create a new token with the same label
2. Update the client to use the new token
3. Revoke the old token

Two tokens with the same label is fine — both show up in the dashboard
list and both log requests under the shared label.

### Direct-origin requests

If `WORKER_ORIGIN_SECRET` is set on the origin, **Cloudflare-fronted**
direct hits to the catch-all (`/v1/*` and anything else not explicitly
handled) return 401 unless they include `X-Worker-Origin-Secret: <secret>`.
"Cloudflare-fronted" is detected by the presence of the `CF-Connecting-IP`
header, which cloudflared injects on every forwarded request.

This carve-out matters in practice: internal Docker callers — an
OpenWebUI container pointed at `http://carbon-proxy:8080/v1`, or any
host on the same LAN/Docker network reaching the origin directly — don't
carry `CF-Connecting-IP`, so the worker-secret check is skipped for
them. Bearer-vs-DB auth still runs via `identify()`. The LAN is treated
as trusted.

For debugging from outside the tunnel (through Cloudflare), supply both
headers manually:

```bash
curl https://carbon.yourdomain.com/v1/models \
  -H "Authorization: Bearer ckp_..." \
  -H "X-Worker-Origin-Secret: <value from origin .env>"
```

`identify()` then runs as usual — bearer → DB lookup → label. Unknown or
revoked bearers fall through to the User-Agent label.

If `WORKER_ORIGIN_SECRET` is left unset (e.g. local Docker dev without a
worker in front), the catch-all is open and labels-only — matching pre-
worker behavior. This is convenient for local iteration but **not** safe
once your origin is publicly tunneled.

The legacy `API_KEYS=` mapping in `.env` is still honored as a fallback
for backwards compatibility but is no longer needed — leave it empty.

### Removing the worker

If you ever take the worker out of the path:

1. Point clients directly at the tunneled origin
2. Bearer tokens still validate via the local DB (`identify()`)
3. KV becomes unused; the reconciler will run but find no work
4. You can leave the CF vars in `.env` or remove them — the reconciler
   no-ops when unconfigured

## Reference: API endpoints

All `/carbon/api_keys` endpoints require `Authorization: Bearer
${OFFSET_API_KEY}` for mutations. `GET` is unauthenticated (assumes Access
is in front).

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/carbon/api_keys?include_revoked=false` | — | array of `{id, label, notes, created_at, last_used_at, revoked_at}` |
| `POST` | `/carbon/api_keys` | `{label, notes?}` | `{...row, token, warning}` — `token` shown once |
| `PATCH` | `/carbon/api_keys/{id}` | `{label?, notes?}` | updated row |
| `DELETE` | `/carbon/api_keys/{id}` | — | revoked row, plus `warning` if KV delete failed |

Internal call paths:

- `POST` → local INSERT → KV PUT → return token (rollback on KV failure)
- `PATCH` (label change) → local UPDATE → KV PUT (drift logged on failure)
- `DELETE` → local UPDATE `revoked_at` → KV DELETE (drift logged on failure)
- Reconciler → enumerate local + KV → re-PUT every active key (idempotent;
  heals stale labels too) and DELETE every KV orphan
