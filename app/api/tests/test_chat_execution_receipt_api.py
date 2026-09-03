"""Execution receipts end to end through the chat endpoint.

``test_execution_receipt.py`` proves the builder. This proves the *wiring*,
which is the part that rots silently: a receipt nobody attaches is worth
nothing, and each of the four ways this app persists an assistant turn
(non-streaming plain, streaming plain, agent tool loop, plain-chat tool loop)
builds its own ``Message``. Mutation testing on the citation receipt showed each
of those could drop its provenance independently with every other test still
green, so each one is driven here and the persisted row is read back through the
API the browser actually uses.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.gateway.client import ChatChunk
from ai4ia_api.main import create_app
from ai4ia_api.memory.in_memory import InMemoryVectorStore
from ai4ia_api.memory.service import MemoryService
from ai4ia_api.websearch.factory import build_web_search_service
from tests.conftest import make_settings, stream_like_gateway
from tests.test_chat_memory_api import KeywordEmbedder
from tests.test_chat_websearch_api import FakeWebClient


class PlainGateway:
    """Answers in one round trip, with Azure's annotate-only filter results."""

    def __init__(self, reply: str = "Sure.", *, annotate: bool = True) -> None:
        self.reply = reply
        self.annotate = annotate
        self.last_messages: list[dict] | None = None

    def _body(self) -> dict:
        body: dict = {
            "choices": [{"message": {"role": "assistant", "content": self.reply}}]
        }
        if self.annotate:
            body["choices"][0]["content_filter_results"] = {
                "violence": {"filtered": False, "severity": "medium"}
            }
        return body

    async def complete(
        self, *, deployment, messages, params=None, correlation_id=None, api="chat"
    ):
        self.last_messages = list(messages)
        return self._body()

    async def stream(
        self, *, deployment, messages, params=None, correlation_id=None, api="chat"
    ):
        self.last_messages = list(messages)
        yield ChatChunk(
            delta=self.reply,
            raw=json.dumps({"choices": [{"delta": {"content": self.reply}}]}),
        )
        yield ChatChunk(done=True, raw="[DONE]")


class AgentToolGateway:
    """Calls @analyst's calculator on the first iteration, then answers."""

    def __init__(
        self,
        reply: str = "Forty-two.",
        *,
        annotate: bool = True,
        **arguments,
    ) -> None:
        self.reply = reply
        self.annotate = annotate
        self.arguments = arguments or {"expression": "6*7"}
        self.iterations = 0

    async def complete(
        self, *, deployment, messages, params=None, correlation_id=None, api="chat"
    ):
        self.iterations += 1
        if self.iterations == 1:
            choice = {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "calculator",
                                "arguments": json.dumps(self.arguments),
                            },
                        }
                    ],
                }
            }
            if self.annotate:
                choice["content_filter_results"] = {
                    "violence": {
                        "filtered": False,
                        "severity": "low",
                    }
                }
            return {
                "choices": [
                    choice
                ]
            }
        choice = {"message": {"role": "assistant", "content": self.reply}}
        if self.annotate:
            choice["content_filter_results"] = {
                "violence": {
                    "filtered": False,
                    "severity": "medium",
                }
            }
        return {"choices": [choice]}

    async def stream(self, **kwargs):
        async for chunk in stream_like_gateway(await self.complete(**kwargs)):
            yield chunk


class PlainToolThenFallbackGateway(PlainGateway):
    """Runs one plain-chat tool, returns no answer, then serves the fallback."""

    def __init__(self) -> None:
        super().__init__("Fallback answer.")
        self.iterations = 0

    async def complete(
        self, *, deployment, messages, params=None, correlation_id=None, api="chat"
    ):
        self.iterations += 1
        if self.iterations == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "search-1",
                                    "type": "function",
                                    "function": {
                                        "name": "web_search",
                                        "arguments": json.dumps(
                                            {"query": "receipt fallback"}
                                        ),
                                    },
                                }
                            ],
                        },
                        "content_filter_results": {
                            "violence": {
                                "filtered": False,
                                "severity": "low",
                            }
                        },
                    }
                ]
            }
        if self.iterations == 2:
            assert any(message.get("role") == "tool" for message in messages)
            return {"choices": [{"message": {"role": "assistant", "content": ""}}]}
        return self._body()


@pytest.fixture
def rc_client():
    app = create_app(make_settings())
    with TestClient(app) as c:
        c.app.state.gateway = PlainGateway()
        yield c


def _session(client: TestClient) -> str:
    resp = client.post("/api/sessions", json={"title": "Chat", "model": "gpt-5.2"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _assistant_row(client: TestClient, sid: str) -> dict:
    resp = client.get(f"/api/sessions/{sid}/messages")
    assert resp.status_code == 200, resp.text
    rows = [m for m in resp.json() if m["role"] == "assistant"]
    assert rows, "expected a persisted assistant message"
    return rows[-1]


def _ask(client: TestClient, sid: str, content: str, *, stream: bool) -> None:
    resp = client.post(
        "/api/chat", json={"sessionId": sid, "content": content, "stream": stream}
    )
    assert resp.status_code == 200, resp.text
    if stream:
        # Drain so the terminal upsert runs inside the response lifecycle.
        assert "[DONE]" in resp.text


# --- The plain paths ---------------------------------------------------------


def test_non_streaming_plain_turn_persists_a_receipt(rc_client):
    sid = _session(rc_client)
    _ask(rc_client, sid, "hello there", stream=False)
    receipt = _assistant_row(rc_client, sid)["executionReceipt"]

    assert receipt is not None
    assert receipt["version"] == 1
    assert receipt["runtime"]["modelId"] == "gpt-5.2"
    assert receipt["runtime"]["deployment"]
    assert receipt["runtime"]["residency"]
    assert receipt["runtime"]["instructionSource"] == "default"
    assert receipt["runtime"]["instructionSha256"]
    assert receipt["status"] == "complete" and receipt["partial"] is False
    # The effective prompt is snapshotted verbatim, so the owner can see what
    # was actually supplied rather than what they typed.
    assert any("hello there" in m["content"]["text"] for m in receipt["prompt"])
    assert receipt["promptBytes"] > 0
    # A plain turn offers no tools. Empty is an assertion here, not an absence.
    assert receipt["toolsOffered"] == [] and receipt["toolCallCount"] == 0
    assert receipt["usage"]["known"] is False
    assert receipt["usage"]["calls"] == 1
    assert receipt["safety"]["status"] == "reported"
    assert receipt["safety"]["coverage"] == ["completion"]


def test_streaming_plain_turn_persists_the_same_receipt_shape(rc_client):
    sid = _session(rc_client)
    _ask(rc_client, sid, "hello there", stream=True)
    row = _assistant_row(rc_client, sid)

    assert row["status"] == "complete"
    receipt = row["executionReceipt"]
    assert receipt is not None
    assert receipt["status"] == "complete" and receipt["partial"] is False
    assert any("hello there" in m["content"]["text"] for m in receipt["prompt"])


def test_prompt_snapshot_records_the_system_prompt_the_user_never_typed(rc_client):
    resp = rc_client.post(
        "/api/sessions",
        json={"title": "Chat", "model": "gpt-5.2", "systemPrompt": "Answer in Latin."},
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]
    _ask(rc_client, sid, "hello", stream=False)
    receipt = _assistant_row(rc_client, sid)["executionReceipt"]

    system = [m for m in receipt["prompt"] if m["role"] == "system"]
    assert any("Answer in Latin." in m["content"]["text"] for m in system)


# --- Automatic context -------------------------------------------------------


def test_recalled_memory_is_recorded_as_admitted_context(rc_client):
    """Memory is injected on the user's behalf without being asked for, which is
    exactly the kind of context a receipt exists to make reviewable."""
    rc_client.app.state.memory = MemoryService(
        store=InMemoryVectorStore(),
        embedder=KeywordEmbedder(),
        min_score=0.5,
        top_k=5,
        min_chars_to_store=8,
    )
    sid = _session(rc_client)
    _ask(rc_client, sid, "My favorite color is orange", stream=False)
    _ask(rc_client, sid, "What is my favorite color?", stream=False)

    receipt = _assistant_row(rc_client, sid)["executionReceipt"]
    memory = [b for b in receipt["contextBlocks"] if b["kind"] == "memory"]
    assert memory, "recalled memory must appear as a context block"
    assert memory[0]["admitted"] is True
    assert "My favorite color is orange" in memory[0]["content"]["text"]
    assert memory[0]["sourceCount"] == 1
    assert memory[0]["sources"][0]["id"]
    assert memory[0]["sources"][0]["version"] == "1"


def test_a_turn_with_no_injected_context_records_no_blocks(rc_client):
    """Control for the block list: it must be able to be empty, or "memory was
    admitted" would carry no information."""
    sid = _session(rc_client)
    _ask(rc_client, sid, "hello", stream=False)
    receipt = _assistant_row(rc_client, sid)["executionReceipt"]
    assert receipt["contextBlocks"] == []


# --- Offered vs. invoked, through the agent tool loop ------------------------


def _run_agent(
    reply: str,
    *,
    stream: bool,
    use_tool: bool = True,
    annotate: bool = True,
    **arguments,
):
    app = create_app(make_settings())
    with TestClient(app) as client:
        gateway = AgentToolGateway(reply, annotate=annotate, **arguments)
        if not use_tool:
            # Answer immediately: the tools are still advertised, the model just
            # declines them.
            gateway.iterations = 1
        client.app.state.gateway = gateway
        sid = _session(client)
        resp = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": "@analyst what is 6*7?", "stream": stream},
        )
        assert resp.status_code == 200, resp.text
        if stream:
            assert "[DONE]" in resp.text
        return _assistant_row(client, sid), gateway


def test_agent_turn_separates_the_tools_offered_from_the_one_invoked():
    used, gateway = _run_agent("Forty-two.", stream=False)
    # The control: identical setup, identical offer, and the model declines the
    # tool. Without it, "calculator was offered" would be indistinguishable from
    # "calculator was invoked" and the offered list would carry no information.
    unused, quiet_gateway = _run_agent("Forty-two.", stream=False, use_tool=False)

    # Non-vacuity for the path: a second iteration means the tool loop really ran.
    assert gateway.iterations >= 2
    assert quiet_gateway.iterations == 2  # the pre-set 1, plus one answer call

    receipt = used["executionReceipt"]
    assert receipt is not None
    offered = {tool["name"] for tool in receipt["toolsOffered"]}
    assert "calculator" in offered
    assert [call["tool"] for call in receipt["toolCalls"]] == ["calculator"]
    assert receipt["toolCalls"][0]["outcome"] == "result"
    assert "6*7" in receipt["toolCalls"][0]["arguments"]["text"]
    assert receipt["toolCalls"][0]["result"]["text"]
    assert receipt["runtime"]["agent"] == "analyst"
    assert receipt["runtime"]["agentConfigSha256"]
    assert receipt["iterations"] >= 2
    assert len(receipt["modelRequests"]) == receipt["iterations"] - 1
    assert "c1" in receipt["modelRequests"][0]["prompt"][-2]["toolCalls"]["text"]
    assert receipt["usage"]["calls"] == 2
    assert receipt["safety"]["signalCount"] == 2

    quiet = unused["executionReceipt"]
    assert "calculator" in {tool["name"] for tool in quiet["toolsOffered"]}
    assert quiet["toolCalls"] == [] and quiet["toolCallCount"] == 0


def test_agent_turn_preserves_safety_from_each_model_call():
    row, _ = _run_agent("Forty-two.", stream=False)

    safety = row["safety"]
    assert safety["status"] == "reported"
    assert sorted(
        [
        (signal["severity"], signal["modelCall"])
        for signal in safety["signals"]
        if signal["category"] == "violence"
        ],
        key=lambda item: item[1],
    ) == [("low", 1), ("medium", 2)]


def test_agent_streaming_turn_records_the_same_calls():
    row, gateway = _run_agent("Forty-two.", stream=True)

    assert gateway.iterations >= 2
    receipt = row["executionReceipt"]
    assert receipt is not None
    assert [call["tool"] for call in receipt["toolCalls"]] == ["calculator"]
    assert "calculator" in {tool["name"] for tool in receipt["toolsOffered"]}
    assert receipt["status"] == "complete"
    assert sorted(
        [
        signal["modelCall"] for signal in row["safety"]["signals"]
        ]
    ) == [1, 2]
    assert row["safety"]["provider"] == "azure_openai"


def test_tool_arguments_are_redacted_before_they_are_persisted():
    """The receipt is written to Cosmos, so a credential in a model-authored
    tool argument must not survive the trip."""
    row, _ = _run_agent(
        "Done.", stream=False, expression="6*7", api_key="sk-live-supersecret"
    )
    receipt = row["executionReceipt"]
    arguments = receipt["toolCalls"][0]["arguments"]["text"]

    assert "sk-live-supersecret" not in arguments
    assert "REDACTED" in arguments
    # Control: the non-secret argument is still legible, so this is redaction
    # rather than the payload having gone missing.
    assert "6*7" in arguments


# --- The plain-chat tool loop ------------------------------------------------


def test_plain_chat_tool_loop_records_its_offered_tools():
    app = create_app(make_settings())
    with TestClient(app) as client:
        client.app.state.gateway = PlainGateway("Here you go.")
        client.app.state.web_search = build_web_search_service(
            make_settings(web_search_enabled=True),
            entitlements=client.app.state.entitlements,
            metering=client.app.state.usage,
            client=FakeWebClient(),
        )
        sid = _session(client)
        _ask(client, sid, "what is the news today?", stream=False)
        receipt = _assistant_row(client, sid)["executionReceipt"]

    assert receipt is not None
    offered = {tool["name"] for tool in receipt["toolsOffered"]}
    assert "web_search" in offered
    # The model answered without calling anything, which is exactly the case the
    # activity trace cannot distinguish from "no tools were available".
    assert receipt["toolCalls"] == []


def test_plain_tool_fallback_retains_tool_receipt_and_safety_annotations():
    app = create_app(make_settings())
    with TestClient(app) as client:
        gateway = PlainToolThenFallbackGateway()
        client.app.state.gateway = gateway
        client.app.state.web_search = build_web_search_service(
            make_settings(web_search_enabled=True),
            entitlements=client.app.state.entitlements,
            metering=client.app.state.usage,
            client=FakeWebClient(),
        )
        sid = _session(client)
        _ask(client, sid, "search then answer", stream=False)
        row = _assistant_row(client, sid)

    receipt = row["executionReceipt"]
    assert gateway.iterations == 3
    assert receipt["iterations"] == 3
    assert [call["tool"] for call in receipt["toolCalls"]] == ["web_search"]
    assert receipt["toolsOffered"]
    assert receipt["usage"]["calls"] == 3
    assert row["safety"]["status"] == "reported"
    assert sorted(
        [
            (signal["severity"], signal["modelCall"])
            for signal in row["safety"]["signals"]
            if signal["category"] == "violence"
        ],
        key=lambda item: item[1],
    ) == [("low", 1), ("medium", 3)]


# --- Safety coverage ---------------------------------------------------------


def test_plain_turn_carries_the_reported_assessment_with_an_ordinal(rc_client):
    sid = _session(rc_client)
    _ask(rc_client, sid, "hello", stream=False)
    safety = _assistant_row(rc_client, sid)["safety"]

    assert safety["status"] == "reported"
    assert safety["provider"] == "azure_openai"
    assert safety["coverage"] == ["completion"]
    signal = next(s for s in safety["signals"] if s["category"] == "violence")
    assert signal["severity"] == "medium" and signal["severityLevel"] == 2


def test_a_turn_with_no_annotations_says_so_rather_than_omitting_the_panel(rc_client):
    """Same path, annotations withheld. Under an annotate-only policy an omitted
    panel reads as "nothing was flagged", which is a claim nobody made."""
    rc_client.app.state.gateway = PlainGateway(annotate=False)
    sid = _session(rc_client)
    _ask(rc_client, sid, "hello", stream=False)
    safety = _assistant_row(rc_client, sid)["safety"]

    assert safety is not None, "the absence of an assessment must be recorded"
    assert safety["status"] == "unavailable"
    assert safety["signals"] == []


def test_agent_turn_reports_its_missing_assessment():
    row, _ = _run_agent("Forty-two.", stream=False, annotate=False)
    assert row["safety"] is not None
    assert row["safety"]["status"] == "unavailable"


# --- No chain of thought -----------------------------------------------------


def test_the_persisted_row_has_no_chain_of_thought_field(rc_client):
    sid = _session(rc_client)
    _ask(rc_client, sid, "hello", stream=False)
    serialized = json.dumps(_assistant_row(rc_client, sid)).lower()

    for forbidden in ("reasoning", "chainofthought", "scratchpad", "thinking"):
        assert forbidden not in serialized
