"""The agent-callable ``process_document`` capability + shared service + serve endpoint.

Mirrors ``test_image_tool``/``test_video_tool``: covers the shared governance core
(:class:`DocumentProcessingService` — instruction/mode validation, the ready-library
reuse gate, upstream-error sanitization, output cap, anti-injection fence), the
synthetic capability's contract (inline vs over-cap-artifact, per-turn cap,
entitlement gate, media-ref sink, metering), the per-user ownership boundary of the
authenticated serve endpoint, and that a document reference round-trips through
``Message`` serialization. All IO is injected (in-memory library + blob + a fake
gateway); no network.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.agents.tool_exec import (
    SELECTABLE_SYNTHETIC_TOOL_NAMES,
    ToolContext,
)
from ai4ia_api.catalog import DeploymentOption
from ai4ia_api.docprocessing.artifacts import DocumentArtifactStore
from ai4ia_api.docprocessing.capability import (
    MAX_PROCESSES_PER_TURN,
    build_document_processing_capability,
)
from ai4ia_api.docprocessing.service import (
    PROCESS_DOCUMENT_TOOL_NAME,
    DocumentProcessingError,
    DocumentProcessingService,
)
from ai4ia_api.gateway.client import ModelGatewayError
from ai4ia_api.library.blob_store import PARSED_NAME, InMemoryBlobStore, blob_path
from ai4ia_api.library.memory_repo import InMemoryDocumentLibraryRepository
from ai4ia_api.library.models import DocumentStatus, UserDocument
from ai4ia_api.library.retrieval import DocumentRetrievalService
from ai4ia_api.main import create_app
from ai4ia_api.sessions.models import Message, MessageAttachment, MessageRole
from tests.conftest import make_settings

DEPLOY = DeploymentOption(region="eastus", sku="GlobalStandard", deploymentName="gpt-test")


class FakeProcessingGateway:
    """Stand-in for ModelGatewayClient.complete: canned text/usage, or raises."""

    def __init__(self, text="ANALYSIS RESULT", usage=None, raise_exc=None):
        self.text = text
        self.usage = usage
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.calls.append({"deployment": deployment, "messages": messages, "api": api})
        if self.raise_exc is not None:
            raise self.raise_exc
        result = {"choices": [{"message": {"role": "assistant", "content": self.text}}]}
        if self.usage is not None:
            result["usage"] = self.usage
        return result


class FakeEntitlements:
    def __init__(self, allowed=True, reason=None):
        self.allowed = allowed
        self.reason = reason
        self.checked: list[str] = []

    async def check(self, user_id):
        self.checked.append(user_id)
        return SimpleNamespace(allowed=self.allowed, reason=self.reason)


class FakeMetering:
    def __init__(self):
        self.calls: list[dict] = []

    async def record_completion(self, **kwargs):
        self.calls.append(kwargs)


def _settings(**overrides):
    base = dict(document_understanding_enabled=True)
    base.update(overrides)
    return make_settings(**base)


def _service(library, blob, gateway, settings):
    retrieval = DocumentRetrievalService(
        library=library, blob_store=blob, chunk_store=None, embedder=None, settings=settings
    )
    return DocumentProcessingService(retrieval=retrieval, gateway=gateway, settings=settings)


def _capability(service, store, settings, *, entitlements=None, metering=None, user_id="u1"):
    sink: list[MessageAttachment] = []
    tools, handlers = build_document_processing_capability(
        processing_service=service,
        artifact_store=store,
        entitlements=entitlements or FakeEntitlements(),
        metering=metering or FakeMetering(),
        deployment=DEPLOY,
        model_id="gpt-test",
        user_id=user_id,
        session_id="s-doc",
        settings=settings,
        sink=sink,
    )
    return tools, handlers, sink


async def _seed_doc(
    library,
    blob,
    *,
    user="u1",
    status=DocumentStatus.ready,
    filename="report.pdf",
    parsed="The quarterly revenue was $30. Costs were $10.",
    doc_id=None,
):
    doc = UserDocument(userId=user, filename=filename, status=status, summary="seed")
    if doc_id:
        doc.id = doc_id
    if parsed is not None:
        path = blob_path(user, doc.id, PARSED_NAME)
        await blob.put(path, parsed.encode("utf-8"), "text/markdown")
        doc.parsedPath = path
    await library.create_document(doc)
    return doc


# --------------------------------------------------------------------------- #
# Service: governance core
# --------------------------------------------------------------------------- #


async def test_service_processes_ready_document():
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    gw = FakeProcessingGateway(text="Revenue: $30", usage={"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16})
    settings = _settings()
    service = _service(library, blob, gw, settings)
    doc = await _seed_doc(library, blob)

    res = await service.process(
        user_id="u1",
        document_id=doc.id,
        instruction="What was revenue?",
        deployment=DEPLOY,
        model_id="gpt-test",
    )
    assert res.text == "Revenue: $30"
    assert res.mode == "analyze"
    assert res.filename == "report.pdf"
    assert res.document_id == doc.id
    assert res.usage.total == 16
    # The call ran on the supplied deployment via the chat-completions surface.
    assert gw.calls[0]["deployment"] == "gpt-test"
    assert gw.calls[0]["api"] == "chat"


async def test_service_rejects_empty_instruction():
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    service = _service(library, blob, FakeProcessingGateway(), _settings())
    doc = await _seed_doc(library, blob)
    with pytest.raises(DocumentProcessingError) as ei:
        await service.process(
            user_id="u1", document_id=doc.id, instruction="   ",
            deployment=DEPLOY, model_id="gpt-test",
        )
    assert ei.value.status_code == 422


async def test_service_rejects_unsupported_mode():
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    service = _service(library, blob, FakeProcessingGateway(), _settings())
    doc = await _seed_doc(library, blob)
    with pytest.raises(DocumentProcessingError) as ei:
        await service.process(
            user_id="u1", document_id=doc.id, instruction="go", mode="translate",
            deployment=DEPLOY, model_id="gpt-test",
        )
    assert ei.value.status_code == 422


async def test_service_not_ready_document_is_sanitized_404():
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    service = _service(library, blob, FakeProcessingGateway(), _settings())
    doc = await _seed_doc(library, blob, status=DocumentStatus.analyzing)
    with pytest.raises(DocumentProcessingError) as ei:
        await service.process(
            user_id="u1", document_id=doc.id, instruction="go",
            deployment=DEPLOY, model_id="gpt-test",
        )
    assert ei.value.status_code == 404


async def test_service_cross_user_cannot_read():
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    service = _service(library, blob, FakeProcessingGateway(), _settings())
    doc = await _seed_doc(library, blob, user="owner")
    # A different user asking for the owner's document id gets a generic not-found.
    with pytest.raises(DocumentProcessingError) as ei:
        await service.process(
            user_id="intruder", document_id=doc.id, instruction="go",
            deployment=DEPLOY, model_id="gpt-test",
        )
    assert ei.value.status_code == 404


@pytest.mark.parametrize(
    "status,expected",
    [(400, 400), (401, 502), (403, 502), (429, 429), (500, 502)],
)
async def test_service_sanitizes_upstream_errors(status, expected):
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    gw = FakeProcessingGateway(raise_exc=ModelGatewayError(status, "raw upstream detail"))
    service = _service(library, blob, gw, _settings())
    doc = await _seed_doc(library, blob)
    with pytest.raises(DocumentProcessingError) as ei:
        await service.process(
            user_id="u1", document_id=doc.id, instruction="go",
            deployment=DEPLOY, model_id="gpt-test",
        )
    assert ei.value.status_code == expected


async def test_service_caps_output_length():
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    gw = FakeProcessingGateway(text="X" * 5000)
    settings = _settings(document_processing_max_output_chars=100)
    service = _service(library, blob, gw, settings)
    doc = await _seed_doc(library, blob)
    res = await service.process(
        user_id="u1", document_id=doc.id, instruction="go",
        deployment=DEPLOY, model_id="gpt-test",
    )
    assert len(res.text) == 100


async def test_service_fences_untrusted_document_in_system_turn():
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    gw = FakeProcessingGateway()
    service = _service(library, blob, gw, _settings())
    doc = await _seed_doc(library, blob, parsed="SECRET BODY TEXT")
    await service.process(
        user_id="u1", document_id=doc.id, instruction="summarize",
        deployment=DEPLOY, model_id="gpt-test",
    )
    messages = gw.calls[0]["messages"]
    system = next(m for m in messages if m["role"] == "system")
    user = next(m for m in messages if m["role"] == "user")
    # Untrusted body lives in the (fenced) system turn; the user turn is only the
    # instruction, so a crafted document can never become the user's request.
    assert "SECRET BODY TEXT" in system["content"]
    assert "BEGIN DOCUMENT" in system["content"]
    assert user["content"] == "summarize"
    assert "SECRET BODY TEXT" not in user["content"]


# --------------------------------------------------------------------------- #
# Capability: synthetic tool contract
# --------------------------------------------------------------------------- #


async def test_capability_schema_and_name():
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    service = _service(library, blob, FakeProcessingGateway(), _settings())
    store = DocumentArtifactStore(InMemoryBlobStore())
    tools, handlers, _ = _capability(service, store, _settings())
    assert tools[0]["function"]["name"] == PROCESS_DOCUMENT_TOOL_NAME
    required = tools[0]["function"]["parameters"]["required"]
    assert required == ["document_id", "instruction"]
    assert set(handlers) == {PROCESS_DOCUMENT_TOOL_NAME}


async def test_handler_inline_result_no_artifact():
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    gw = FakeProcessingGateway(text="short answer")
    settings = _settings()
    service = _service(library, blob, gw, settings)
    store = DocumentArtifactStore(InMemoryBlobStore())
    metering = FakeMetering()
    doc = await _seed_doc(library, blob)
    tools, handlers, sink = _capability(service, store, settings, metering=metering)

    out = await handlers[PROCESS_DOCUMENT_TOOL_NAME](
        {"document_id": doc.id, "instruction": "what?"}, ToolContext()
    )
    assert out["status"] == "processed"
    assert out["result"] == "short answer"
    assert "artifact_id" not in out
    assert sink == []  # small result is inline, no attachment
    assert len(metering.calls) == 1  # the model round-trip was metered


async def test_handler_overcap_result_persists_and_sinks():
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    long_text = "Y" * 4000
    gw = FakeProcessingGateway(text=long_text)
    settings = _settings(document_processing_inline_max_chars=50)
    service = _service(library, blob, gw, settings)
    store = DocumentArtifactStore(InMemoryBlobStore())
    doc = await _seed_doc(library, blob, filename="big.csv")
    tools, handlers, sink = _capability(service, store, settings)

    out = await handlers[PROCESS_DOCUMENT_TOOL_NAME](
        {"document_id": doc.id, "instruction": "extract", "mode": "extract"}, ToolContext()
    )
    assert out["status"] == "processed"
    artifact_id = out["artifact_id"]
    # The full text never returns through the tool channel; only a bounded preview.
    assert "result" not in out
    assert len(out["preview"]) < len(long_text)
    # A document reference was appended for the chat router to attach.
    assert len(sink) == 1
    att = sink[0]
    assert att.id == artifact_id
    assert att.kind == "document"
    assert att.filename == "big.csv"
    assert att.mimeType == "text/markdown"
    # The full result is durably stored, owner-scoped.
    stored = await store.get("u1", artifact_id)
    assert stored.decode("utf-8") == long_text


async def test_handler_blocks_disabled_user():
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    service = _service(library, blob, FakeProcessingGateway(), _settings())
    store = DocumentArtifactStore(InMemoryBlobStore())
    doc = await _seed_doc(library, blob)
    tools, handlers, sink = _capability(
        service, store, _settings(),
        entitlements=FakeEntitlements(allowed=False, reason="disabled"),
    )
    out = await handlers[PROCESS_DOCUMENT_TOOL_NAME](
        {"document_id": doc.id, "instruction": "go"}, ToolContext()
    )
    assert "error" in out
    assert sink == []


async def test_handler_enforces_per_turn_budget():
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    service = _service(library, blob, FakeProcessingGateway(text="ok"), _settings())
    store = DocumentArtifactStore(InMemoryBlobStore())
    doc = await _seed_doc(library, blob)
    tools, handlers, sink = _capability(service, store, _settings())
    handler = handlers[PROCESS_DOCUMENT_TOOL_NAME]
    for _ in range(MAX_PROCESSES_PER_TURN):
        ok = await handler({"document_id": doc.id, "instruction": "go"}, ToolContext())
        assert ok["status"] == "processed"
    over = await handler({"document_id": doc.id, "instruction": "go"}, ToolContext())
    assert "error" in over


async def test_handler_rejects_missing_args():
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    service = _service(library, blob, FakeProcessingGateway(), _settings())
    store = DocumentArtifactStore(InMemoryBlobStore())
    tools, handlers, _ = _capability(service, store, _settings())
    handler = handlers[PROCESS_DOCUMENT_TOOL_NAME]
    assert "error" in await handler({"instruction": "go"}, ToolContext())
    assert "error" in await handler({"document_id": "x"}, ToolContext())


async def test_handler_sanitizes_document_error():
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    service = _service(library, blob, FakeProcessingGateway(), _settings())
    store = DocumentArtifactStore(InMemoryBlobStore())
    # A not-ready document yields a sanitized error string, never an exception.
    doc = await _seed_doc(library, blob, status=DocumentStatus.failed)
    tools, handlers, sink = _capability(service, store, _settings())
    out = await handlers[PROCESS_DOCUMENT_TOOL_NAME](
        {"document_id": doc.id, "instruction": "go"}, ToolContext()
    )
    assert "error" in out
    assert sink == []


# --------------------------------------------------------------------------- #
# Serve endpoint: per-user ownership boundary
# --------------------------------------------------------------------------- #


def _client() -> TestClient:
    app = create_app(make_settings(admin_subjects="alice"))
    c = TestClient(app)
    c.__enter__()
    return c


@pytest.fixture
def client():
    c = _client()
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def _internal_id(client, headers) -> str:
    return client.get("/api/entitlement", headers=headers).json()["userId"]


def _make_artifact(client, user_id: str) -> str:
    """Run the handler to persist an over-cap artifact into the app's store."""
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    settings = make_settings(document_understanding_enabled=True, document_processing_inline_max_chars=10)
    gw = FakeProcessingGateway(text="Z" * 2000)
    service = _service(library, blob, gw, settings)
    doc = asyncio.run(_seed_doc(library, blob, user=user_id))
    sink: list[MessageAttachment] = []
    _, handlers = build_document_processing_capability(
        processing_service=service,
        artifact_store=client.app.state.document_artifacts,
        entitlements=client.app.state.entitlements,
        metering=client.app.state.usage,
        deployment=DEPLOY,
        model_id="gpt-test",
        user_id=user_id,
        session_id="s-doc",
        settings=settings,
        sink=sink,
    )
    out = asyncio.run(
        handlers[PROCESS_DOCUMENT_TOOL_NAME](
            {"document_id": doc.id, "instruction": "extract"}, ToolContext()
        )
    )
    return out["artifact_id"]


def test_serve_endpoint_owner_can_read_others_cannot(client):
    owner = {"X-Dev-User": "owner"}
    other = {"X-Dev-User": "intruder"}
    owner_id = _internal_id(client, owner)
    artifact_id = _make_artifact(client, owner_id)

    r_owner = client.get(f"/api/documents/artifacts/{artifact_id}", headers=owner)
    assert r_owner.status_code == 200
    assert r_owner.headers["content-type"].startswith("text/markdown")
    assert r_owner.text == "Z" * 2000

    # A different user guessing the same id gets a 404, never a cross-user read.
    r_other = client.get(f"/api/documents/artifacts/{artifact_id}", headers=other)
    assert r_other.status_code == 404


def test_serve_endpoint_rejects_malformed_id(client):
    r = client.get(
        "/api/documents/artifacts/..%2f..%2fetc", headers={"X-Dev-User": "ian"}
    )
    assert r.status_code in (404, 400)


def test_artifact_id_pattern_matches_only_a_uuid4_hex_token():
    # Every real artifact id is uuid4().hex: exactly 32 lowercase hex chars.
    # A too-short or too-long hex run must not match, even though it would
    # still 404 (via blob-not-found) either way over HTTP.
    from ai4ia_api.routers.docprocessing import _ARTIFACT_ID_RE

    assert _ARTIFACT_ID_RE.match("a" * 32)
    assert not _ARTIFACT_ID_RE.match("a" * 31)
    assert not _ARTIFACT_ID_RE.match("a" * 33)
    assert not _ARTIFACT_ID_RE.match("a" * 8)


def test_serve_endpoint_unknown_id_404(client):
    r = client.get(
        "/api/documents/artifacts/" + "a" * 32, headers={"X-Dev-User": "ian"}
    )
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Registration + Message serialization
# --------------------------------------------------------------------------- #


def test_process_document_is_user_selectable():
    assert PROCESS_DOCUMENT_TOOL_NAME in SELECTABLE_SYNTHETIC_TOOL_NAMES


def test_document_attachment_round_trips():
    msg = Message(
        sessionId="s1",
        userId="u1",
        role=MessageRole.assistant,
        content="done",
        attachments=[
            MessageAttachment(
                id="b" * 32,
                kind="document",
                mimeType="text/markdown",
                prompt="extract the totals",
                model="gpt-test",
                filename="report.pdf",
            )
        ],
    )
    doc = msg.model_dump(mode="json")
    restored = Message.model_validate(doc)
    assert restored.attachments[0].kind == "document"
    assert restored.attachments[0].filename == "report.pdf"
    assert restored.attachments[0].mimeType == "text/markdown"
