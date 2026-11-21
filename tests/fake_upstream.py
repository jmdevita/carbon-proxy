"""Fake llama-swap upstream for exercising carbon-proxy edge cases."""
import asyncio
import gzip
import json

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

STATE = {"slow_cancelled": False, "slow_finished": False}


def sse(obj, event=None):
    head = f"event: {event}\n" if event else ""
    return (head + "data: " + json.dumps(obj) + "\n\n").encode()


async def chat(request):
    body = await request.json()
    kind = body.get("kind", "normal")

    async def normal():
        for i in range(3):
            yield sse({"choices": [{"delta": {"content": "x"}}]})
            await asyncio.sleep(0.05)
        yield sse({"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7}})
        yield b"data: [DONE]\n\n"

    async def slow():
        try:
            for i in range(100):
                yield sse({"choices": [{"delta": {"content": "x"}}]})
                await asyncio.sleep(0.1)
            STATE["slow_finished"] = True
        except asyncio.CancelledError:
            STATE["slow_cancelled"] = True
            raise

    async def die():
        yield sse({"choices": [{"delta": {"content": "x"}}]})
        await asyncio.sleep(0.1)
        raise RuntimeError("upstream crashed mid-stream")

    if not body.get("stream"):
        if kind == "gzip":
            raw = gzip.compress(json.dumps({"usage": {"prompt_tokens": 5, "completion_tokens": 3}, "pad": "y" * 500}).encode())
            return Response(raw, media_type="application/json", headers={"content-encoding": "gzip"})
        return JSONResponse({"usage": {"prompt_tokens": 5, "completion_tokens": 3}})
    gen = {"normal": normal, "slow": slow, "die": die}[kind]
    return StreamingResponse(gen(), media_type="text/event-stream")


async def messages(request):
    async def gen():
        yield sse({"type": "message_start", "message": {"usage": {"input_tokens": 21, "output_tokens": 1}}}, "message_start")
        yield sse({"type": "content_block_delta", "delta": {"text": "hi"}}, "content_block_delta")
        yield sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 9}}, "message_delta")
        yield sse({"type": "message_stop"}, "message_stop")
    return StreamingResponse(gen(), media_type="text/event-stream")


async def responses(request):
    async def gen():
        yield sse({"type": "response.output_text.delta", "delta": "hi"}, "response.output_text.delta")
        yield sse({"type": "response.completed", "response": {"usage": {"input_tokens": 31, "output_tokens": 13}}}, "response.completed")
    return StreamingResponse(gen(), media_type="text/event-stream")


async def state(request):
    return JSONResponse(STATE)


app = Starlette(routes=[
    Route("/v1/chat/completions", chat, methods=["POST"]),
    Route("/v1/messages", messages, methods=["POST"]),
    Route("/v1/responses", responses, methods=["POST"]),
    Route("/_state", state),
])
