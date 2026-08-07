"""Measure time-to-first-token on a tool-using turn, before and after P1-16.

Runs the REAL ``POST /api/chat`` streaming path (router + runtime + SSE framing)
against a gateway double that PACES its chunks, so the numbers reflect the
application's behaviour under a model that emits tokens over time rather than
all at once. It is deliberately not a claim about a live deployment: no Foundry
model is reachable from a test environment, so the model's own generation rate is
simulated. What is real is *where the application chooses to wait*, and that is
the entire content of the finding.

It runs a real **uvicorn** server and a real HTTP client, NOT
``fastapi.testclient.TestClient``. That is not incidental: Starlette's test
transport runs the ASGI app to completion and buffers the whole body before the
first line is readable, so measuring through it reports identical TTFT for both
arms and hides exactly the effect under test. Measured, not assumed -- the first
version of this harness used TestClient and reported a 1.0x "improvement".

Method
------
One turn: iteration 1 emits N text tokens then asks for ``calculator``;
iteration 2 emits N more. Each token costs ``--token-ms``; the tool itself is
instant. TTFT is measured from request send to the first SSE frame carrying
``choices[0].delta.content``.

The "before" arm is produced by the shipped kill switch
(``gateway_stream_tool_loop=False``) rather than by a stashed checkout, so both
arms run identical code and differ only in the one flag under test.

Usage::

    python benchmarks/ttft_tool_turn.py --token-ms 20 --runs 5
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import socket
import statistics
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from ai4ia_api.gateway.client import ChatChunk  # noqa: E402
from ai4ia_api.main import create_app  # noqa: E402
from tests.conftest import make_settings  # noqa: E402

TOKENS = [f"word{i} " for i in range(20)]
# Local dev auth: the Next.js proxy is the authority for this header in the real
# app; here the harness is the client, so it supplies one directly.
_DEV_USER = "bench@example.com"


class PacedGateway:
    """A model that emits one token every ``token_ms``, then calls a tool."""

    def __init__(self, token_ms: float) -> None:
        self._delay = token_ms / 1000.0
        self.calls = 0

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.calls += 1
        first = self.calls == 1
        for _ in TOKENS:
            await asyncio.sleep(self._delay)
        if first:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "".join(TOKENS),
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "calculator",
                                        "arguments": json.dumps({"expression": "6*7"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"role": "assistant", "content": "".join(TOKENS)}}]}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None):
        self.calls += 1
        first = self.calls == 1
        for token in TOKENS:
            await asyncio.sleep(self._delay)
            yield ChatChunk(
                delta=token,
                raw=json.dumps({"choices": [{"delta": {"content": token}}]}),
            )
        if first:
            head = {
                "index": 0,
                "id": "c1",
                "type": "function",
                "function": {"name": "calculator", "arguments": ""},
            }
            yield ChatChunk(raw=json.dumps({"choices": [{"delta": {"tool_calls": [head]}}]}))
            body = {"index": 0, "function": {"arguments": json.dumps({"expression": "6*7"})}}
            yield ChatChunk(raw=json.dumps({"choices": [{"delta": {"tool_calls": [body]}}]}))
        yield ChatChunk(done=True, raw="[DONE]")


async def _measure(base_url: str, session_id: str) -> tuple[float, float]:
    """One turn against a real HTTP server; returns (TTFT ms, total ms)."""
    async with httpx.AsyncClient(base_url=base_url, timeout=60.0) as client:
        start = time.perf_counter()
        first: float | None = None
        async with client.stream(
            "POST",
            "/api/chat",
            headers={"X-Dev-User": _DEV_USER},
            json={
                "sessionId": session_id,
                "content": "@analyst compute 6*7",
                "stream": True,
            },
        ) as response:
            if response.status_code != 200:
                raise SystemExit(f"chat failed: {response.status_code}")
            async for line in response.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                payload = json.loads(line.removeprefix("data: "))
                choices = payload.get("choices")
                if first is None and choices and (choices[0]["delta"].get("content") or ""):
                    first = time.perf_counter()
        if first is None:
            raise SystemExit("no content delta was ever emitted")
        return (first - start) * 1000.0, (time.perf_counter() - start) * 1000.0


@contextlib.contextmanager
def _server(streaming: bool, token_ms: float):
    """Serve the real app on an ephemeral port with a paced gateway installed."""
    app = create_app(make_settings(gateway_stream_tool_loop=streaming))
    app.state.gateway = PacedGateway(token_ms)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, log_level="error", loop="asyncio")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.time() + 20
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        if not server.started:
            raise SystemExit("uvicorn did not start")
        yield f"http://127.0.0.1:{port}", app
    finally:
        server.should_exit = True
        thread.join(timeout=10)


async def _arm(streaming: bool, token_ms: float, runs: int) -> tuple[list[float], list[float]]:
    ttfts: list[float] = []
    totals: list[float] = []
    for _ in range(runs):
        with _server(streaming, token_ms) as (base_url, app):
            async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
                created = await client.post(
                    "/api/sessions",
                    headers={"X-Dev-User": _DEV_USER},
                    json={"title": "T", "model": "gpt-5.2"},
                )
                if created.status_code != 201:
                    raise SystemExit(f"session create failed: {created.text}")
                session_id = created.json()["id"]
            # A fresh gateway per run so iteration counting starts at 1.
            app.state.gateway = PacedGateway(token_ms)
            ttft, total = await _measure(base_url, session_id)
        ttfts.append(ttft)
        totals.append(total)
    return ttfts, totals


def main() -> int:
    parser = argparse.ArgumentParser(description="TTFT on a tool-using turn")
    parser.add_argument("--token-ms", type=float, default=20.0)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()

    before_ttft, before_total = asyncio.run(_arm(False, args.token_ms, args.runs))
    after_ttft, after_total = asyncio.run(_arm(True, args.token_ms, args.runs))

    def line(label: str, values: list[float]) -> str:
        return (
            f"{label:<26} median {statistics.median(values):8.1f} ms   "
            f"min {min(values):8.1f}   max {max(values):8.1f}"
        )

    print(
        f"tool-using turn: {len(TOKENS)} tokens/iteration, 2 iterations, "
        f"{args.token_ms} ms/token, {args.runs} runs/arm"
    )
    print(line("TTFT  before (flag off)", before_ttft))
    print(line("TTFT  after  (flag on)", after_ttft))
    print(line("total before (flag off)", before_total))
    print(line("total after  (flag on)", after_total))
    before_median = statistics.median(before_ttft)
    after_median = statistics.median(after_ttft)
    print(
        f"\nTTFT improvement: {before_median / after_median:.1f}x "
        f"({before_median - after_median:.0f} ms sooner)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
