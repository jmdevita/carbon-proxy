import hashlib
import logging

from starlette.requests import Request

import db
from config import get_api_key_map

logger = logging.getLogger("carbon-proxy.client_id")

# Matches the label cap in api/api_keys.py. A key's label must survive intact
# as the request source, since per-key stats and last_used_at join on it.
MAX_LABEL_LEN = 64


def identify(request: Request, body: dict | None = None) -> str:
    # 1. API key from Authorization header (only present on direct origin hits;
    #    requests through the Cloudflare worker have Authorization stripped and
    #    arrive with X-Carbon-Source pre-set instead).
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        key = auth[7:].strip()

        # 1a. Dashboard-issued tokens live in the api_keys table by hash.
        try:
            key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
            row = db.api_key_lookup_active(key_hash)
            if row:
                # last_used_at is stamped by db.log_request, keyed on this
                # label, so worker-validated requests update it too.
                return row["label"][:MAX_LABEL_LEN]
        except Exception as e:
            logger.debug("api_keys DB lookup failed: %s", e)

        # 1b. Legacy static .env map (kept for back-compat; safe to leave empty).
        source = get_api_key_map().get(key)
        if source:
            return source

    # 2. X-Carbon-Source custom header (set by the Cloudflare worker after
    #    edge-side validation, or by any client that wants to self-identify).
    carbon_source = request.headers.get("x-carbon-source", "").strip()
    if carbon_source:
        return carbon_source[:MAX_LABEL_LEN]

    # 3. User-Agent
    ua = request.headers.get("user-agent", "")
    if ua:
        # Truncate to something readable
        return ua.split("/")[0].strip()[:50] or "unknown"

    return "unknown"
