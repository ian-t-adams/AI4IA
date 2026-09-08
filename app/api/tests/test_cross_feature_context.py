"""Integration rehearsal for memory fences, Search coverage and request evidence."""
from __future__ import annotations

import json

import httpx
import pytest

from ai4ia_api.gateway.client import ModelGatewayClient
from tests.test_ai_search_chunks import _FakeIndexClient
from tests.test_chat_library_api import _make_client, _seed_ready_doc
from tests.test_doc_retrieval import FakeEmbedder, UnavailableSearchClient
from tests.test_memory_preference import cosmos_memory, set_automatic
from tests.test_memory_preference_execution import HEADERS, OWNER_FACT


@pytest.mark.parametrize("preference", ["on", "off", "off_on"])
@pytest.mark.parametrize("search_unavailable", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_memory_fence_and_search_status_describe_the_same_actual_request(
    monkeypatch, preference, search_unavailable, stream,
):
    requests: list[dict] = []

    async def provider(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        usage = {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
        if body.get("stream"):
            chunks = [
                {"choices": [{"delta": {"content": "An observed answer."}}]},
                {"choices": [], "usage": usage},
            ]
            events = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                content=events + "data: [DONE]\n\n",
            )
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "An observed answer."}}],
            "usage": usage,
        })

    client = _make_client(
        document_understanding_enabled=True,
        search_endpoint="https://example.search.windows.net",
        memory_embedding_dimensions=3,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        try:
            memory, _store, _embedder, _planner, _container = cosmos_memory()
            client.app.state.memory = memory
            client.app.state.gateway = ModelGatewayClient(client.app.state.settings, http)
            created = client.post("/api/memories", headers=HEADERS, json={"text": OWNER_FACT})
            assert created.status_code == 201, created.text
            response = client.post("/api/sessions", headers=HEADERS, json={
                "model": "gpt-5.2", "title": "Combined context",
            })
            assert response.status_code == 201, response.text
            session = response.json()
            await _seed_ready_doc(client, session["userId"])
            search = UnavailableSearchClient()
            search.unavailable = search_unavailable
            chunks = client.app.state.document_ingestor.chunks
            chunks._injected_search_client = search
            chunks._index_client = _FakeIndexClient()
            client.app.state.document_retrieval._embedder = FakeEmbedder()
            original = client.app.state.session_repo.patch_session
            checkpoints = []

            async def after_context(*args, **kwargs):
                result = await original(*args, **kwargs)
                checkpoints.append(True)
                if preference != "on":
                    await set_automatic(memory, session["userId"], False)
                    if preference == "off_on":
                        await set_automatic(memory, session["userId"], True)
                return result

            monkeypatch.setattr(client.app.state.session_repo, "patch_session", after_context)
            response = client.post("/api/chat", headers=HEADERS, json={
                "sessionId": session["id"],
                "content": "What is the Falcon status and which answer style do I prefer?",
                "stream": stream,
                "params": {"max_tokens": 64, "reasoning_effort": "low"},
            })
            assert response.status_code == 200, response.text
            assert checkpoints
            assert search.search_calls
            assert len(requests) == 1
            actual = requests[0]
            systems = "\n".join(
                message["content"] for message in actual["messages"] if message["role"] == "system"
            )
            assert (OWNER_FACT in systems) is (preference == "on")
            assert "Project Falcon status brief" in systems
            assert ("Library retrieval is unavailable" in systems) is search_unavailable
            assert actual["max_completion_tokens"] == 64
            assert actual["reasoning_effort"] == "low"

            messages = client.get(
                f"/api/sessions/{session['id']}/messages", headers=HEADERS,
            ).json()
            assistant = [message for message in messages if message["role"] == "assistant"][-1]
            receipt = assistant["executionReceipt"]
            memory_block = next(block for block in receipt["contextBlocks"] if block["kind"] == "memory")
            assert memory_block["admitted"] is (preference == "on")
            assert (
                any(OWNER_FACT in (message["content"]["text"] or "") for message in receipt["prompt"])
            ) is (preference == "on")
            assert ("library_retrieval_unavailable" in receipt["notes"]) is search_unavailable
            assert receipt["partial"] is search_unavailable
            assert receipt["usage"]["totalTokens"] == 14
            assert "PRIVATE QUERY AND SOURCE CONTENT" not in json.dumps(receipt)
        finally:
            client.__exit__(None, None, None)
