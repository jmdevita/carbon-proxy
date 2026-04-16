from starlette.requests import Request
from config import get_api_key_map


def identify(request: Request, body: dict | None = None) -> str:
    # 1. API key from Authorization header
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        key = auth[7:].strip()
        source = get_api_key_map().get(key)
        if source:
            return source

    # 2. user field in request body
    if body and body.get("user"):
        return str(body["user"])

    # 3. User-Agent
    ua = request.headers.get("user-agent", "")
    if ua:
        # Truncate to something readable
        return ua.split("/")[0].strip()[:50] or "unknown"

    return "unknown"
