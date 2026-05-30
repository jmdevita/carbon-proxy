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
| `POST /v1/...` (proxy catch-all) | **`X-Worker-Origin-Secret` header**, when `WORKER_ORIGIN_SECRET` is set | Yes (bearer ↔ KV) | n/a (worker fronts this) |
| `GET /dashboard`, `GET /carbon/*` reporting | **none** | n/a (not fronted) | **Yes, only if you set it up** |
| `POST/PATCH/DELETE /carbon/api_keys/*`, `POST /carbon/offset`, `POST /carbon/auto_offset/toggle` | `OFFSET_API_KEY` bearer, always | n/a | Yes, if set up |

Three things follow from this:

1. **The proxy catch-all is gated by a shared secret with the worker.** The
   worker stamps every forwarded request with `X-Worker-Origin-Secret`;
   origin rejects any catch-all request missing or mismatching it. This
   closes the "direct curl to the tunnel hostname for free inference"
   bypass. If `WORKER_ORIGIN_SECRET` is left empty (e.g. local dev), the
   check is skipped — the catch-all becomes open again.
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
   once in the origin `.env` and once in `wrangler.toml`

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

## 4. Write the worker

Create a new directory (e.g. `carbon-proxy-auth/`) with this `src/worker.js`:

```javascript
async function sha256Hex(s) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
}

function unauthorized() {
  return new Response(JSON.stringify({ error: "Unauthorized" }), {
    status: 401,
    headers: { "Content-Type": "application/json" },
  });
}

export default {
  async fetch(request, env) {
    const auth = request.headers.get("Authorization") || "";
    const token = auth.startsWith("Bearer ") ? auth.slice(7).trim() : null;
    if (!token) return unauthorized();

    // KV stores sha256(token) → label. The worker hashes incoming bearers so
    // KV never holds plaintext (defense-in-depth against a KV dump leak).
    const hash = await sha256Hex(token);
    const label = await env.API_KEYS.get(hash, { cacheTtl: 60 });
    if (!label) return unauthorized();

    // Forward to origin: strip Authorization, set X-Carbon-Source, and
    // prove this came through the worker (so direct-origin curls 401).
    const fwd = new Request(
      request.url.replace(env.PUBLIC_HOST, env.ORIGIN_HOST),
      request
    );
    fwd.headers.delete("Authorization");
    fwd.headers.set("X-Carbon-Source", label);
    if (env.ORIGIN_SECRET) {
      fwd.headers.set("X-Worker-Origin-Secret", env.ORIGIN_SECRET);
    }
    return fetch(fwd);
  },
};
```

Notes:

- `cacheTtl: 60` is the per-edge read cache. Lower means faster revocation
  globally at the cost of more KV reads; 60 seconds is a good default.
- The hostname rewrite is the only place the worker knows about your
  topology. Both hostnames are passed as `vars` in `wrangler.jsonc` below.
- `ORIGIN_SECRET` is a wrangler **secret** (not in `vars`), set via
  `wrangler secret put ORIGIN_SECRET`. The same value goes in the origin's
  `.env` as `WORKER_ORIGIN_SECRET`. The `if (env.ORIGIN_SECRET)` guard
  keeps the worker working even before you provision the secret.

## 5. Configure wrangler

Create `wrangler.toml`:

```toml
name = "carbon-proxy-auth"
main = "src/worker.js"
compatibility_date = "2025-01-01"

[vars]
PUBLIC_HOST = "api-keys.yourdomain.com"
ORIGIN_HOST = "api.yourdomain.com"

[[kv_namespaces]]
binding = "API_KEYS"             # must match env.API_KEYS in worker.js
id      = "<KV_NAMESPACE_ID>"    # from step 1
```

Deploy:

```bash
wrangler login
wrangler deploy
```

## 6. Route traffic through the worker

You have two reasonable options.

### Option A: Worker route on a separate hostname (recommended)

The worker lives at `api-keys.yourdomain.com`, the tunneled origin at
`api.yourdomain.com`. Clients talk to `api-keys.*`; only the worker talks to
`api.*`.

In the CF dashboard:

1. **Workers & Pages** → your worker → **Settings** → **Triggers** →
   **Add Custom Domain**: `api-keys.yourdomain.com`
2. CF creates the DNS record automatically

This keeps the public LLM API and the tunneled origin on distinct hostnames,
which is easier to reason about than "same hostname, worker selectively
forwards."

### Option B: Worker route on the same hostname

If you prefer a single hostname, add a `[[routes]]` section to
`wrangler.toml`:

```toml
[[routes]]
pattern = "api.yourdomain.com/v1/*"
zone_name = "yourdomain.com"
```

The worker now intercepts only `/v1/*`; the dashboard and `/carbon/*` go
straight to origin via tunnel. Worker code stays the same.

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
   curl https://api-keys.yourdomain.com/v1/models \
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

If `WORKER_ORIGIN_SECRET` is set on the origin, direct hits to the catch-all
(`/v1/*` and anything else not explicitly handled) return 401 unless they
include `X-Worker-Origin-Secret: <secret>`. For debugging from inside the
tunnel network, supply both headers manually:

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
