"""Bearer checks shared by the management endpoints."""
import hmac

from fastapi import HTTPException, Request

from config import settings


def secrets_match(provided: str, expected: str) -> bool:
    """Constant-time comparison, so response timing leaks nothing about the secret."""
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def require_admin(request: Request, feature: str) -> None:
    """Require `Authorization: Bearer $OFFSET_API_KEY`.

    OFFSET_API_KEY guards every mutating endpoint (offset purchases, the
    auto-offset toggle, API key management). `feature` names the caller in the
    403 returned when no key is configured.
    """
    if not settings.offset_api_key:
        raise HTTPException(403, f"{feature} disabled -- set OFFSET_API_KEY to enable")
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or not secrets_match(
        auth[7:].strip(), settings.offset_api_key
    ):
        raise HTTPException(401, "Invalid or missing management API key")
