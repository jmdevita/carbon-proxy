"""Cloudflare Workers KV REST client.

The worker reads KV via its binding to authenticate bearer tokens at the edge.
This module is how the origin pushes / removes / lists entries from outside CF.

Keys are SHA-256 hashes of the plaintext bearer token (never the plaintext).
Values are the human-readable label, sent to the origin as X-Carbon-Source.

KV consistency: writes propagate to all edges in ~60s; reads cache up to ~60s.
Practical effect: revocation is best-effort within ~1 minute globally.
"""
import logging

import httpx

from config import settings, kv_is_configured

logger = logging.getLogger("carbon-proxy.cloudflare_kv")

_client: httpx.AsyncClient | None = None


def _base_url() -> str:
    return (
        f"https://api.cloudflare.com/client/v4/accounts/"
        f"{settings.cloudflare_account_id}/storage/kv/namespaces/"
        f"{settings.kv_namespace_id}"
    )


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.cloudflare_api_token}"}


def init_client():
    global _client
    if _client is None and kv_is_configured():
        _client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))


async def close_client():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _require_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError(
            "Cloudflare KV is not configured. Set CLOUDFLARE_API_TOKEN, "
            "CLOUDFLARE_ACCOUNT_ID, and KV_NAMESPACE_ID."
        )
    return _client


async def put(key: str, value: str) -> None:
    """Write a single key. Raises on non-2xx."""
    client = _require_client()
    r = await client.put(
        f"{_base_url()}/values/{key}",
        headers=_headers(),
        content=value.encode("utf-8"),
    )
    r.raise_for_status()


async def delete(key: str) -> None:
    """Delete a single key. 404 is treated as success (already gone)."""
    client = _require_client()
    r = await client.delete(f"{_base_url()}/values/{key}", headers=_headers())
    if r.status_code == 404:
        return
    r.raise_for_status()


async def list_keys() -> set[str]:
    """Enumerate all keys in the namespace (paginated). Used by the reconciler."""
    client = _require_client()
    keys: set[str] = set()
    cursor: str | None = None
    while True:
        params = {"limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = await client.get(
            f"{_base_url()}/keys", headers=_headers(), params=params
        )
        r.raise_for_status()
        data = r.json()
        for entry in data.get("result", []):
            keys.add(entry["name"])
        cursor = (data.get("result_info") or {}).get("cursor") or None
        if not cursor:
            break
    return keys
