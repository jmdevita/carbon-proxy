"""API key management.

Tokens are issued from the dashboard and validated at the Cloudflare worker
via Workers KV. Origin is the source of truth (SQLite); KV is a projection.

Wire shape:
- Token plaintext: "ckp_" + 32 url-safe bytes. Shown to the user exactly once.
- KV key:          sha256(plaintext).hex()  -- the worker hashes incoming bearers.
- KV value:        the human label (becomes X-Carbon-Source on forwarded requests).

Failure model:
- POST: local INSERT, then KV PUT. If KV fails, local row is deleted -> 502.
- DELETE: local revoke (soft), then KV DELETE. KV failure leaves drift for the
  reconciler to clean up next tick; the local row stays revoked.
- Reconciler: periodically ensures every active local hash is in KV and every
  revoked local hash is absent. Drift is logged.
"""
import asyncio
import hashlib
import logging
import secrets

from fastapi import APIRouter, Body, HTTPException, Request

import db
from config import settings, kv_is_configured
from integrations import cloudflare_kv

logger = logging.getLogger("carbon-proxy.api_keys")

router = APIRouter(prefix="/carbon/api_keys", tags=["api_keys"])

# Serializes reconciler ticks so a manual run can't race the background loop.
_reconcile_lock = asyncio.Lock()


def _hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _check_auth(request: Request):
    """Reuse the OFFSET_API_KEY bearer pattern from /carbon/offset."""
    if not settings.offset_api_key:
        raise HTTPException(
            403,
            "API key management disabled -- set OFFSET_API_KEY to enable.",
        )
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[7:].strip() != settings.offset_api_key:
        raise HTTPException(401, "Invalid or missing management API key.")


def _public_row(row: dict) -> dict:
    """Strip the hash from outputs -- the dashboard never needs it."""
    return {
        "id": row["id"],
        "label": row["label"],
        "notes": row.get("notes", ""),
        "created_at": row["created_at"],
        "last_used_at": row.get("last_used_at"),
        "revoked_at": row.get("revoked_at"),
    }


@router.get("/_client_config")
async def client_config():
    """Small bag of values the dashboard needs to render snippets.

    Lives under /carbon/api_keys to keep the namespace cohesive. Underscore
    prefix avoids any chance of a future numeric key_id colliding.
    """
    return {"public_api_url": settings.public_api_url}


@router.get("")
async def list_keys(include_revoked: bool = False):
    rows = await asyncio.to_thread(db.api_key_list, include_revoked)
    return [_public_row(r) for r in rows]


@router.post("")
async def create_key(
    request: Request,
    label: str = Body(..., embed=True),
    notes: str = Body("", embed=True),
):
    """Create a new API key. Returns the plaintext token exactly once."""
    _check_auth(request)

    # Validate input before checking infrastructure -- "label required" is a
    # more actionable error than "KV not configured" when both are wrong.
    label = label.strip()[:64]
    if not label:
        raise HTTPException(400, "Label is required.")

    if not kv_is_configured():
        raise HTTPException(
            503,
            "Cloudflare KV is not configured. Set CLOUDFLARE_API_TOKEN, "
            "CLOUDFLARE_ACCOUNT_ID, and KV_NAMESPACE_ID.",
        )

    # Token format: ckp_ + 32 url-safe bytes (~43 chars). Recognizable prefix
    # so leaked tokens are scannable in logs / secret scanners.
    plaintext = "ckp_" + secrets.token_urlsafe(32)
    key_hash = _hash_token(plaintext)

    key_id = await asyncio.to_thread(
        db.api_key_create, key_hash, label, notes.strip()[:255]
    )

    # Push to KV. If this fails, roll back the local row so the user doesn't
    # walk away with a token that will 401 at the edge.
    try:
        await cloudflare_kv.put(key_hash, label, metadata={"label": label})
    except Exception as e:
        await asyncio.to_thread(db.api_key_delete_local, key_hash)
        logger.error("KV write failed during create, rolled back local row: %s", e)
        raise HTTPException(502, f"Failed to sync key to Cloudflare KV: {e}")

    row = await asyncio.to_thread(db.api_key_get, key_id)
    logger.info("API key created: id=%d label=%s", key_id, label)
    return {
        **_public_row(row),
        # Plaintext token is returned once and never again.
        "token": plaintext,
        "warning": "Save this token now -- it will not be shown again.",
    }


@router.get("/{key_id}/stats")
async def key_stats(key_id: int, range: str = "7d"):
    """Per-key rollup powering the card sparkline + drill-down.

    `range` is currently advisory -- we always compute a 7-day window because
    the sparkline expects exactly 7 daily buckets. Kept as a query param so
    we can extend to 30d/90d later without breaking the URL shape.
    """
    row = await asyncio.to_thread(db.api_key_get, key_id)
    if not row:
        raise HTTPException(404, "API key not found.")

    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    stats = await asyncio.to_thread(db.api_key_stats, row["label"], since)
    return stats


@router.patch("/{key_id}")
async def update_key(
    key_id: int,
    request: Request,
    label: str | None = Body(None, embed=True),
    notes: str | None = Body(None, embed=True),
):
    """Update label and/or notes. KV value is the label, so sync if changed."""
    _check_auth(request)
    before = await asyncio.to_thread(db.api_key_get, key_id)
    if not before:
        raise HTTPException(404, "API key not found.")

    clean_label = label.strip()[:64] if label is not None else None
    clean_notes = notes.strip()[:255] if notes is not None else None

    row = await asyncio.to_thread(
        db.api_key_update_label, key_id, clean_label, clean_notes
    )

    # If label changed and the key is still active, update the KV value.
    if (
        clean_label is not None
        and clean_label != before["label"]
        and before.get("revoked_at") is None
        and kv_is_configured()
    ):
        try:
            await cloudflare_kv.put(
                before["key_hash"], clean_label, metadata={"label": clean_label}
            )
        except Exception as e:
            # Drift: log and let the reconciler heal it next tick.
            logger.warning(
                "KV label update failed for id=%d (reconciler will retry): %s",
                key_id, e,
            )

    return _public_row(row)


@router.delete("/{key_id}")
async def revoke_key(key_id: int, request: Request):
    """Soft-revoke (sets revoked_at) and removes from KV."""
    _check_auth(request)
    before = await asyncio.to_thread(db.api_key_get, key_id)
    if not before:
        raise HTTPException(404, "API key not found.")

    row = await asyncio.to_thread(db.api_key_revoke, key_id)

    kv_warning = None
    if kv_is_configured():
        try:
            await cloudflare_kv.delete(before["key_hash"])
        except Exception as e:
            # Reconciler will retry. Don't fail the request -- local row is
            # already revoked, so identify()/lookup will reject it on direct
            # origin hits even if KV still has it briefly.
            kv_warning = f"KV delete failed: {e}. Reconciler will retry."
            logger.warning("KV delete failed for id=%d: %s", key_id, e)

    logger.info("API key revoked: id=%d label=%s", key_id, before["label"])
    out = _public_row(row)
    if kv_warning:
        out["warning"] = kv_warning
    return out


async def reconcile_once() -> dict:
    """Heal drift between local DB and KV.

    - PUT any active key whose KV label is missing or stale.
    - DELETE any KV key not active locally.

    Drift detection is done by comparing local labels to the label stored
    in KV metadata, which `list_entries()` returns alongside each key. In
    steady state this loop performs one list and zero writes per tick,
    independent of how many active keys exist.
    """
    async with _reconcile_lock:
        if not kv_is_configured():
            return {"skipped": "kv not configured"}

        active_map = await asyncio.to_thread(db.api_key_active_map)
        kv_entries = await cloudflare_kv.list_entries()

        active_hashes = set(active_map.keys())
        kv_hashes = set(kv_entries.keys())
        missing_from_kv = active_hashes - kv_hashes
        orphan_in_kv = kv_hashes - active_hashes
        stale_labels = {
            h for h in active_hashes & kv_hashes
            if kv_entries[h] != active_map[h]
        }

        pushed = 0
        for h in missing_from_kv | stale_labels:
            label = active_map[h]
            try:
                await cloudflare_kv.put(h, label, metadata={"label": label})
                pushed += 1
            except Exception as e:
                logger.warning("Reconciler put failed for %s: %s", h[:8], e)

        for h in orphan_in_kv:
            try:
                await cloudflare_kv.delete(h)
            except Exception as e:
                logger.warning("Reconciler delete failed for %s: %s", h[:8], e)

        result = {
            "active_local": len(active_hashes),
            "kv_keys": len(kv_hashes),
            "pushed_to_kv": pushed,
            "added_to_kv": len(missing_from_kv),
            "stale_labels_fixed": len(stale_labels),
            "removed_from_kv": len(orphan_in_kv),
        }
        if missing_from_kv or orphan_in_kv or stale_labels:
            logger.info("Reconciler healed drift: %s", result)
        return result


async def reconciler_loop():
    """Background reconciliation. Mirrors auto_offset.push_loop pattern."""
    if not kv_is_configured():
        logger.info("KV not configured -- reconciler disabled.")
        return
    interval = settings.kv_reconciler_interval_s
    logger.info("KV reconciler started (every %ds)", interval)
    while True:
        try:
            await reconcile_once()
        except asyncio.CancelledError:
            logger.info("KV reconciler cancelled")
            raise
        except Exception as e:
            logger.warning("Reconciler tick failed: %s", e)
        await asyncio.sleep(interval)
