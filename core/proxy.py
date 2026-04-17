import asyncio
import json
import time
import logging

import httpx
from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse, Response
from starlette.background import BackgroundTask

from config import settings
from core.client_id import identify
import db
from energy.power import monitor as power_monitor
from energy.carbon import joules_to_kwh, calculate_co2

logger = logging.getLogger("carbon-proxy.proxy")

router = APIRouter()


def _extract_tokens(data: dict) -> tuple[int, int]:
    """Extract token counts from response data, supporting all backend formats.

    Checks usage dict (OpenAI/Anthropic) and top-level fields (Ollama/llama.cpp native).
    """
    usage = data.get("usage", {})

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
    return int(tokens_in), int(tokens_out)

# Shared httpx client, initialized in main.py lifespan
http_client: httpx.AsyncClient = None


def init_client():
    global http_client
    http_client = httpx.AsyncClient(
        base_url=settings.upstream_url,
        timeout=httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0),
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

        if parsed_body:
            source = identify(request, parsed_body)[:255]
            model = (parsed_body.get("model", "") or "")[:255]
            is_streaming = parsed_body.get("stream", False)

            # Inject stream_options to get usage in final chunk (OpenAI-compatible)
            if is_streaming and "stream_options" not in parsed_body:
                parsed_body["stream_options"] = {"include_usage": True}
                body = json.dumps(parsed_body).encode("utf-8")
        else:
            source = identify(request)

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
        return await _handle_non_streaming(upstream_req, source, model, start_time, request_id)
    else:
        return await _handle_passthrough(upstream_req)


async def _handle_passthrough(upstream_req: httpx.Request) -> Response:
    resp = await http_client.send(upstream_req, stream=True)
    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers=dict(resp.headers),
        background=BackgroundTask(resp.aclose),
    )


async def _handle_non_streaming(
    upstream_req: httpx.Request,
    source: str,
    model: str,
    start_time: float,
    request_id: str,
) -> Response:
    resp = await http_client.send(upstream_req)
    duration_ms = int((time.monotonic() - start_time) * 1000)
    body = resp.content

    tokens_in = 0
    tokens_out = 0
    content_type = resp.headers.get("content-type", "")
    if "json" in content_type or "text" in content_type:
        try:
            data = json.loads(body)
            tokens_in, tokens_out = _extract_tokens(data)
        except Exception:
            pass  # Non-JSON or no usage data -- that's fine

    energy = power_monitor.end_request(request_id, tokens_out)
    energy_kwh = joules_to_kwh(energy.energy_joules)
    co2_grams = await asyncio.to_thread(calculate_co2, energy_kwh)

    await _log_request(
        source, model, tokens_in, tokens_out, duration_ms,
        energy.energy_joules, co2_grams, energy.power_source,
    )

    return Response(
        content=body,
        status_code=resp.status_code,
        headers=dict(resp.headers),
    )


async def _handle_streaming(
    upstream_req: httpx.Request,
    source: str,
    model: str,
    start_time: float,
    request_id: str,
) -> StreamingResponse:
    resp = await http_client.send(upstream_req, stream=True)

    # Mutable state shared between generator and background task
    stream_state = {"last_data_line": "", "remainder": ""}

    async def stream_with_capture():
        async for chunk in resp.aiter_raw():
            yield chunk

            try:
                # Scan for last data line containing usage
                text = stream_state["remainder"] + chunk.decode("utf-8", errors="replace")
                lines = text.split("\n")
                # Keep incomplete last line for next chunk (cap at 64KB)
                stream_state["remainder"] = lines[-1][:65536]
                for line in lines[:-1]:
                    stripped = line.strip()
                    if stripped.startswith("data: ") and stripped != "data: [DONE]":
                        stream_state["last_data_line"] = stripped
            except Exception:
                pass  # Never interrupt the stream for usage extraction

    async def cleanup_and_log():
        await resp.aclose()

        duration_ms = int((time.monotonic() - start_time) * 1000)
        tokens_in = 0
        tokens_out = 0

        last_data_line = stream_state["last_data_line"]
        if last_data_line:
            try:
                data = json.loads(last_data_line[6:])  # Strip "data: " prefix
                tokens_in, tokens_out = _extract_tokens(data)
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Failed to extract usage from streaming response: %s", e)

        energy = power_monitor.end_request(request_id, tokens_out)
        energy_kwh = joules_to_kwh(energy.energy_joules)
        co2_grams = await asyncio.to_thread(calculate_co2, energy_kwh)

        await _log_request(
            source, model, tokens_in, tokens_out, duration_ms,
            energy.energy_joules, co2_grams, energy.power_source,
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
):
    logger.info(
        "request source=%s model=%s tokens_in=%d tokens_out=%d duration_ms=%d energy=%.2fJ co2=%.4fg power=%s",
        source, model, tokens_in, tokens_out, duration_ms, energy_joules, co2_grams, power_source,
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
    )
