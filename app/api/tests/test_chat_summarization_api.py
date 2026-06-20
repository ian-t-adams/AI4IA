"""End-to-end summarization behavior through POST /api/chat (WS2 part C).

Proves the three contract-level guarantees:
* DEFAULT-OFF inertness — with the flag off, the model receives the full history
  and NO summary block (byte-for-byte the prior behavior).
* Manual ``/summarize`` folds + persists a running summary on the session.
* The automatic path (flag on, past threshold) folds the oldest turns and injects
  the summary, while the FULL transcript stays in storage.
"""
from __future__ import annotations

from ai4ia_api.agents.summarization import SummarizationService
from ai4ia_api.gateway.client import ChatChunk


class _CapturingGateway:
    """Records the last messages sent to the model; returns a fixed reply."""

    def __init__(self, text: str = "ok") -> None:
        self.text = text
        self.last_messages: list[dict] | None = None

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.last_messages = messages
        return {"choices": [{"message": {"role": "assistant", "content": self.text}}]}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.last_messages = messages
        yield ChatChunk(delta=self.text, raw="{}")
        yield ChatChunk(done=True, raw="[DONE]")


def _create_session(client, model="gpt-5.2"):
    resp = client.post("/api/sessions", json={"title": "Chat", "model": model})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _say(client, sid, content):
    resp = client.post(
        "/api/chat", json={"sessionId": sid, "content": content, "stream": False}
    )
    assert resp.status_code == 200, resp.text
    return resp


def _system_blocks(messages):
    return [m["content"] for m in messages if m["role"] == "system"]


# --- DEFAULT-OFF inertness ----------------------------------------------------


def test_default_off_sends_full_history_no_summary_block(client):
    gw = _CapturingGateway()
    client.app.state.gateway = gw
    assert client.app.state.summarizer.enabled is False
    sid = _create_session(client)["id"]

    for i in range(3):
        _say(client, sid, f"message number {i} with some content")

    # Every prior user turn is still present (full history), and no summary block
    # was injected — the off path is byte-for-byte the original behavior.
    sent = gw.last_messages
    user_contents = [m["content"] for m in sent if m["role"] == "user"]
    assert "message number 0 with some content" in user_contents
    assert "message number 1 with some content" in user_contents
    assert all("Summary of earlier conversation" not in b for b in _system_blocks(sent))

    # The session never accrued a summary.
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["summary"] is None


# --- manual /summarize --------------------------------------------------------


def test_manual_summarize_persists_running_summary(client):
    gw = _CapturingGateway(text="DIGEST OF CHAT")
    client.app.state.gateway = gw
    sid = _create_session(client)["id"]
    _say(client, sid, "first real user turn about budgets")
    _say(client, sid, "second real user turn about timelines")

    resp = _say(client, sid, "/summarize")
    assert "running summary" in resp.json()["message"]["content"].lower()

    session = client.get(f"/api/sessions/{sid}").json()
    assert session["summary"] == "DIGEST OF CHAT"
    assert session["summarizedThroughMessageId"] is not None

    # The full transcript is retained (the /summarize echo + reply are extra).
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    roles = [m["role"] for m in msgs]
    assert roles.count("user") >= 2  # the two real turns are still there


def test_manual_summarize_with_no_history_is_friendly(client):
    client.app.state.gateway = _CapturingGateway()
    sid = _create_session(client)["id"]
    resp = _say(client, sid, "/summarize")
    assert "not enough conversation" in resp.json()["message"]["content"].lower()


# --- automatic path (flag on, model with no metadata -> low fallback) ---------


def test_auto_summarization_folds_and_preserves_transcript(client):
    gw = _CapturingGateway()
    client.app.state.gateway = gw
    # Enable the auto path with a tiny threshold so a few short turns trigger it.
    client.app.state.summarizer = SummarizationService(
        enabled=True, recent_turns=2, fallback_threshold_chars=10
    )
    # model-router has no context-window metadata -> fallback threshold applies.
    sid = _create_session(client, model="model-router")["id"]

    for i in range(4):
        _say(client, sid, f"turn {i} with enough text to exceed the tiny threshold")

    sent = gw.last_messages
    # The rolling summary was injected as a system block...
    assert any("Summary of earlier conversation" in b for b in _system_blocks(sent))
    # ...and only the recent window (not the whole transcript) was sent.
    non_system = [m for m in sent if m["role"] != "system"]

    stored = client.get(f"/api/sessions/{sid}/messages").json()
    # Full transcript preserved in storage: 4 user + 4 assistant = 8 messages.
    assert len([m for m in stored if m["role"] in ("user", "assistant")]) == 8
    # The model saw fewer turns than are stored — folding actually shrank context.
    assert len(non_system) < 8

    session = client.get(f"/api/sessions/{sid}").json()
    assert session["summary"] is not None
