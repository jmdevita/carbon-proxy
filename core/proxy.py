import asyncio
import json
import re
import time
import logging

import httpx
from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse, Response
from starlette.background import BackgroundTask

from api.auth import secrets_match
from config import settings
from core.client_id import identify
import db
from energy.power import monitor as power_monitor
from energy.carbon import joules_to_kwh, calculate_co2

logger = logging.getLogger("carbon-proxy.proxy")

router = APIRouter()


def _extract_tokens(data: dict) -> tuple[int, int, int]:
    """Extract token counts from response data, supporting all backend formats.

    Returns (fresh_prompt_tokens, completion_tokens, cached_prompt_tokens).
    Cached tokens are subtracted from fresh — they're KV-cache prefix hits with
    near-zero compute, tracked separately for visibility but excluded from totals.
    """
    # Usage sits at the top level for OpenAI chat and Anthropic message_delta,
    # under "message" for Anthropic message_start, and under "response" for
    # the Responses API's response.completed event.
    usage = (
        data.get("usage")
        or (data.get("message") or {}).get("usage")
        or (data.get("response") or {}).get("usage")
        or {}
    )

    tokens_in = (
        usage.get("prompt_tokens")
        or usage.get("input_tokens")
        or data.get("prompt_eval_count")  # Ollama native
        or data.get("tokens_evaluated")   # llama.cpp /completion
        or 0
    )
    tokens_out = (
        usage.get("completion_tokens")
        or usage.get("output_tokens")
        or data.get("eval_count")          # Ollama native
        or data.get("tokens_predicted")    # llama.cpp /completion
        or 0
    )
    # `or {}` guards against explicit null (some upstreams send
    # "prompt_tokens_details": null, where .get(default) wouldn't kick in).
    details = usage.get("prompt_tokens_details") or {}
    cached_tokens = int(details.get("cached_tokens") or 0)
    fresh_tokens_in = max(0, int(tokens_in) - cached_tokens)
    return fresh_tokens_in, int(tokens_out), cached_tokens


# A streamed event is only worth JSON-parsing if it can carry token counts.
# The last two are llama.cpp's native /completion fields.
_USAGE_MARKERS = ('"usage"', '"tokens_predicted"', '"tokens_evaluated"')


def _merge_stream_usage(totals: dict, data_line: str) -> None:
    """Fold one SSE `data: ` line's token counts into `totals`.

    Usage can be split across events (Anthropic sends input tokens in
    message_start and output tokens in message_delta) and is not always in the
    last event, so each non-zero count overwrites the previous one.
    """
    if not any(marker in data_line for marker in _USAGE_MARKERS):
        return
    try:
        data = json.loads(data_line[6:])  # Strip "data: " prefix
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    tokens_in, tokens_out, cached_tokens = _extract_tokens(data)
    if tokens_in:
        totals["tokens_in"] = tokens_in
    if tokens_out:
        totals["tokens_out"] = tokens_out
    if cached_tokens:
        totals["cached_tokens"] = cached_tokens


def _multipart_field(body: bytes, content_type: str, name: str) -> str:
    """Return a plain (non-file) field from a multipart/form-data body, or "".

    Only the target field is decoded; file parts are skipped without being
    copied. Used to read `model` from OpenAI /v1/images/edits uploads, which
    are multipart rather than JSON.
    """
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    if not match:
        return ""
    delimiter = b"--" + match.group(1).encode("latin-1")
    for part in body.split(delimiter)[1:]:
        head, sep, value = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        disposition = next(
            (line for line in head.decode("latin-1").split("\r\n")
             if line.lower().startswith("content-disposition:")),
            "",
        )
        if f'name="{name}"' in disposition and "filename=" not in disposition:
            return value.removesuffix(b"\r\n").decode("utf-8", errors="replace").strip()
    return ""


# Shared httpx client, initialized in main.py lifespan
http_client: httpx.AsyncClient = None


def init_client():
    global http_client
    http_client = httpx.AsyncClient(
        base_url=settings.upstream_url,
        # read must cover a full non-streaming generation; long thinking
        # responses on slow dense models can exceed 10 minutes
        timeout=httpx.Timeout(connect=30.0, read=1200.0, write=30.0, pool=30.0),
    )


async def close_client():
    global http_client
    if http_client:
        await http_client.aclose()


# Headers to not forward between client and upstream
HOP_BY_HOP = frozenset({
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
    "content-length",
})


def _forward_headers(headers, exclude=HOP_BY_HOP):
    return [(k, v) for k, v in headers.items() if k.lower() not in exclude]


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy_request(request: Request, path: str):
    # Defense against direct-origin bypass: when configured, requests that
    # came through Cloudflare must carry the worker's shared secret. Through
    # the worker this header is auto-set; direct curls to the tunnel hostname
    # have no way to supply it without insider access.
    #
    # We gate on CF-Connecting-IP because cloudflared sets it on every
    # forwarded request, while internal docker callers (openwebui, local
    # scripts hitting carbon-proxy:8080) never do. Spoofing it from inside
    # the docker network can only force the check ON for yourself -- it
    # can't disable the check for an external attacker, since external
    # traffic always traverses cloudflared.
    came_via_cloudflare = "cf-connecting-ip" in request.headers
    if settings.worker_origin_secret and came_via_cloudflare:
        provided = request.headers.get("x-worker-origin-secret", "")
        if not secrets_match(provided, settings.worker_origin_secret):
            return Response(
                content='{"error":"missing or invalid worker origin secret"}',
                status_code=401,
                media_type="application/json",
            )

    body = await request.body()
    target_path = f"/{path}" if path else "/"
    url = httpx.URL(path=target_path, query=request.url.query.encode() if request.url.query else None)
    headers = _forward_headers(request.headers)

    # Instrument all POST requests -- try to extract usage from any response
    is_post = request.method == "POST"

    parsed_body = None
    source = "unknown"
    model = ""
    is_streaming = False
    start_time = None
    request_id = None

    if is_post and body:
        try:
            parsed_body = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed_body = None

        # Valid JSON that is not an object (a bare array or string) has no
        # model/stream fields; forward it untouched like any other opaque body.
        if isinstance(parsed_body, dict) and parsed_body:
            source = identify(request, parsed_body)[:255]
            model = (parsed_body.get("model", "") or "")[:255]
            is_streaming = parsed_body.get("stream", False)

            # Inject stream_options to get usage in final chunk (OpenAI-compatible)
            if is_streaming and "stream_options" not in parsed_body:
                parsed_body["stream_options"] = {"include_usage": True}
                body = json.dumps(parsed_body).encode("utf-8")
        else:
            source = identify(request)
            content_type = request.headers.get("content-type", "")
            if content_type.startswith("multipart/form-data"):
                model = _multipart_field(body, content_type, "model")[:255]

        start_time = time.monotonic()
        request_id = power_monitor.begin_request()

    upstream_req = http_client.build_request(
        method=request.method,
        url=url,
        headers=headers,
        content=body,
    )

    if is_post and is_streaming:
        return await _handle_streaming(upstream_req, source, model, start_time, request_id)
    elif is_post and body:
        return await _handle_non_streaming(request, upstream_req, source, model, start_time, request_id)
    else:
        return await _handle_passthrough(upstream_req)


def _upstream_error_response(exc: httpx.RequestError) -> Response:
    """Report an unreachable or unresponsive upstream as a gateway error.

    Letting the exception propagate would surface as a bare 500, which reads
    as a carbon-proxy bug instead of "the model server is down".
    """
    timed_out = isinstance(exc, httpx.TimeoutException)
    logger.warning("Upstream request failed: %s: %s", type(exc).__name__, exc)
    return Response(
        content=json.dumps({"error": {
            "message": "Upstream timed out" if timed_out else "Upstream unreachable",
            "type": "upstream_error",
        }}),
        status_code=504 if timed_out else 502,
        media_type="application/json",
    )


async def _handle_passthrough(upstream_req: httpx.Request) -> Response:
    try:
        resp = await http_client.send(upstream_req, stream=True)
    except httpx.RequestError as exc:
        return _upstream_error_response(exc)
    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers=dict(resp.headers),
        background=BackgroundTask(resp.aclose),
    )


async def _wait_for_disconnect(request: Request) -> None:
    """Return once the client has hung up, and never otherwise."""
    try:
        while (await request.receive())["type"] != "http.disconnect":
            pass
    except Exception:
        # Without a working watcher, let the upstream call run to completion
        # rather than cancel it for a client that may still be there.
        logger.exception("Disconnect watcher failed; upstream request will not be cancelled")
        await asyncio.Event().wait()


async def _send_unless_disconnected(request: Request, upstream_req: httpx.Request) -> httpx.Response | None:
    """Send upstream, cancelling it (and returning None) if the client hangs up first.

    Starlette does not cancel a handler when its client disconnects. Without
    this, a client that times out and retries leaves the abandoned generation
    running upstream, holding a slot that its retry then queues behind.
    Cancelling the send closes the upstream connection, which llama-swap and
    llama-server treat as a cancel.
    """
    send_task = asyncio.create_task(http_client.send(upstream_req))
    disconnect_task = asyncio.create_task(_wait_for_disconnect(request))
    try:
        await asyncio.wait({send_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        disconnect_task.cancel()
        if not send_task.done():
            send_task.cancel()
            # Let httpx finish closing the upstream connection before returning
            await asyncio.wait({send_task})
    if send_task.cancelled():
        return None
    return send_task.result()


async def _handle_non_streaming(
    request: Request,
    upstream_req: httpx.Request,
    source: str,
    model: str,
    start_time: float,
    request_id: str,
) -> Response:
    try:
        resp = await _send_unless_disconnected(request, upstream_req)
    except Exception as exc:
        # Upstream failed before we got a response; close the active-request
        # entry so it can't pin TDP-fallback power at the "active" load factor.
        power_monitor.end_request(request_id, 0)
        if isinstance(exc, httpx.RequestError):
            return _upstream_error_response(exc)
        raise

    duration_ms = int((time.monotonic() - start_time) * 1000)

    tokens_in = 0
    tokens_out = 0
    cached_tokens = 0
    content_type = resp.headers.get("content-type", "") if resp is not None else ""
    if "json" in content_type or "text" in content_type:
        try:
            data = json.loads(resp.content)
            tokens_in, tokens_out, cached_tokens = _extract_tokens(data)
        except Exception:
            pass  # Non-JSON or no usage data -- that's fine

    energy = power_monitor.end_request(request_id, tokens_out)
    energy_kwh = joules_to_kwh(energy.energy_joules)
    co2_grams = await asyncio.to_thread(calculate_co2, energy_kwh)

    await _log_request(
        source, model, tokens_in, tokens_out, duration_ms,
        energy.energy_joules, co2_grams, energy.power_source,
        cached_tokens=cached_tokens,
    )

    if resp is None:
        # Client hung up; the energy is logged above, but there is no one to
        # send a response to.
        return Response(status_code=499)

    # resp.content is already decompressed by httpx, so the upstream's
    # content-encoding and content-length no longer describe it. Drop them and
    # let Starlette compute the length of what is actually sent.
    headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
    }
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=headers,
    )


async def _handle_streaming(
    upstream_req: httpx.Request,
    source: str,
    model: str,
    start_time: float,
    request_id: str,
) -> StreamingResponse:
    try:
        resp = await http_client.send(upstream_req, stream=True)
    except Exception as exc:
        # Upstream connection failed; release the active-request slot so a
        # leaked entry can't pin TDP-fallback power at the active load factor.
        power_monitor.end_request(request_id, 0)
        if isinstance(exc, httpx.RequestError):
            return _upstream_error_response(exc)
        raise

    # Mutable state shared between generator and background task
    stream_state = {
        "remainder": "", "logged": False,
        "tokens_in": 0, "tokens_out": 0, "cached_tokens": 0,
    }

    async def stream_with_capture():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk

                try:
                    text = stream_state["remainder"] + chunk.decode("utf-8", errors="replace")
                    lines = text.split("\n")
                    # Keep incomplete last line for next chunk (cap at 64KB)
                    stream_state["remainder"] = lines[-1][:65536]
                    for line in lines[:-1]:
                        stripped = line.strip()
                        if stripped.startswith("data: "):
                            _merge_stream_usage(stream_state, stripped)
                except Exception:
                    pass  # Never interrupt the stream for usage extraction
        except Exception:
            # Upstream died mid-stream. Starlette skips the background task
            # when the body iterator raises, so log here before re-raising or
            # the request stays active (and unlogged) until the reaper.
            await cleanup_and_log()
            raise

    async def cleanup_and_log():
        if stream_state["logged"]:
            return
        stream_state["logged"] = True

        try:
            await resp.aclose()
        except Exception:
            pass

        duration_ms = int((time.monotonic() - start_time) * 1000)
        tokens_in = stream_state["tokens_in"]
        tokens_out = stream_state["tokens_out"]
        cached_tokens = stream_state["cached_tokens"]

        # end_request must run on every path (client disconnect mid-stream,
        # upstream truncation) to avoid leaking active requests, which dilute
        # every other request's energy share until the reaper removes them.
        energy = power_monitor.end_request(request_id, tokens_out)
        energy_kwh = joules_to_kwh(energy.energy_joules)
        co2_grams = await asyncio.to_thread(calculate_co2, energy_kwh)

        await _log_request(
            source, model, tokens_in, tokens_out, duration_ms,
            energy.energy_joules, co2_grams, energy.power_source,
            cached_tokens=cached_tokens,
        )

    return StreamingResponse(
        stream_with_capture(),
        status_code=resp.status_code,
        headers=dict(resp.headers),
        background=BackgroundTask(cleanup_and_log),
    )


async def _log_request(
    source: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    duration_ms: int,
    energy_joules: float = 0.0,
    co2_grams: float = 0.0,
    power_source: str = "none",
    cached_tokens: int = 0,
):
    logger.info(
        "request source=%s model=%s tokens_in=%d tokens_out=%d cached=%d duration_ms=%d energy=%.2fJ co2=%.4fg power=%s",
        source, model, tokens_in, tokens_out, cached_tokens, duration_ms, energy_joules, co2_grams, power_source,
    )
    await db.log_request_async(
        source=source,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        duration_ms=duration_ms,
        energy_joules=energy_joules,
        co2_grams=co2_grams,
        power_source=power_source,
        cached_tokens=cached_tokens,
    )
