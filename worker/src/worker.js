// carbon-proxy-auth: edge bearer validation for the carbon-proxy.
//
// Flow per request:
//   1. Read Authorization: Bearer <token>
//   2. Hash the token (sha256) -- KV stores hashes, never plaintext
//   3. Look up the hash in KV -> label (e.g. "claude-code")
//   4. Strip Authorization, set X-Carbon-Source: <label>, forward to origin
//
// The origin (carbon-proxy) reads X-Carbon-Source in its identify() path and
// logs every request under that label. Origin never sees the bearer.

async function sha256Hex(s) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
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

    // cacheTtl: 60 is the per-edge read-cache window. Lower => faster
    // revocation globally at the cost of more KV reads. 60s is the floor.
    const hash = await sha256Hex(token);
    const label = await env.API_KEYS.get(hash, { cacheTtl: 60 });
    if (!label) return unauthorized();

    // Rewrite the host so the request lands on the tunneled origin. Both
    // hostnames live in wrangler.jsonc `vars`, so this file stays generic.
    const fwd = new Request(
      request.url.replace(env.PUBLIC_HOST, env.ORIGIN_HOST),
      request,
    );
    fwd.headers.delete("Authorization");
    fwd.headers.set("X-Carbon-Source", label);
    // Prove to origin that this request came through the worker. Origin
    // rejects /v1/* without this header, so direct-origin curls can't bypass
    // the bearer check. ORIGIN_SECRET is a wrangler secret (not in vars).
    if (env.ORIGIN_SECRET) {
      fwd.headers.set("X-Worker-Origin-Secret", env.ORIGIN_SECRET);
    }
    return fetch(fwd);
  },
};
