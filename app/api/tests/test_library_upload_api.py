"""Library upload endpoint: default-OFF refusal, the stored-then-enrich
happy path, dedupe, caps (413/409/422), and analyzer validation. CU is not
configured in these settings, so enrichment is an inert background no-op and the
document settles at ``stored`` — exactly the local/default posture."""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.library.ingest import EnrichScheduleOutcome
from ai4ia_api.main import create_app
from tests.conftest import make_settings


def _client(**overrides) -> TestClient:
    app = create_app(make_settings(document_understanding_enabled=True, **overrides))
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


def _upload(client: TestClient, name: str, data: bytes, ctype: str = "text/plain", **form):
    return client.post(
        "/api/library/documents",
        files={"file": (name, data, ctype)},
        data=form,
    )


def test_upload_disabled_by_default_returns_404():
    app = create_app(make_settings())  # document_understanding_enabled defaults False
    with TestClient(app) as c:
        resp = c.post(
            "/api/library/documents",
            files={"file": ("a.txt", b"hi", "text/plain")},
        )
        assert resp.status_code == 404


def test_upload_happy_path_stored(client):
    resp = _upload(client, "note.txt", b"hello world content")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "stored"
    assert body["filename"] == "note.txt"
    assert body["summary"] == "hello world content"
    # It appears in the user's library listing.
    listed = client.get("/api/library/documents").json()
    assert body["id"] in {d["id"] for d in listed}


def test_upload_saturation_settles_failed_instead_of_stored_orphan(
    client, monkeypatch
):
    ingestor = client.app.state.document_ingestor
    ingestor._cu = object()
    monkeypatch.setattr(
        ingestor,
        "schedule_enrich",
        lambda **_kwargs: EnrichScheduleOutcome.saturated,
    )

    response = _upload(client, "busy.txt", b"retryable content")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert "Re-upload to retry" in body["error"]


def test_failed_saturation_patch_returns_retryable_and_identical_upload_reschedules(
    client, monkeypatch
):
    ingestor = client.app.state.document_ingestor
    ingestor._cu = object()
    outcomes = iter(
        [EnrichScheduleOutcome.saturated, EnrichScheduleOutcome.scheduled]
    )
    schedule_calls: list[str] = []

    def schedule(**kwargs):
        schedule_calls.append(kwargs["document_id"])
        return next(outcomes)

    async def failed_patch(doc, changes, *, require_status=None):
        return "error", None

    monkeypatch.setattr(ingestor, "schedule_enrich", schedule)
    monkeypatch.setattr(ingestor, "_safe_update", failed_patch)

    first = _upload(client, "busy.txt", b"stored for retry")
    second = _upload(client, "busy.txt", b"stored for retry")

    assert first.status_code == 503
    assert first.headers["retry-after"] == "5"
    assert second.status_code == 201
    assert second.json()["status"] == "stored"
    assert len(schedule_calls) == 2
    assert schedule_calls[0] == schedule_calls[1]


def test_identical_stored_upload_with_already_running_task_is_noop(client, monkeypatch):
    ingestor = client.app.state.document_ingestor
    ingestor._cu = object()
    monkeypatch.setattr(
        ingestor,
        "schedule_enrich",
        lambda **_kwargs: EnrichScheduleOutcome.already_running,
    )

    first = _upload(client, "running.txt", b"same in-flight bytes")
    second = _upload(client, "running.txt", b"same in-flight bytes")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


def test_upload_dedupe_returns_same_document(client):
    first = _upload(client, "a.txt", b"identical bytes")
    second = _upload(client, "a.txt", b"identical bytes")
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get("/api/library/documents").json()) == 1


def test_upload_empty_file_422(client):
    resp = _upload(client, "empty.txt", b"")
    assert resp.status_code == 422


def test_upload_too_large_413():
    c = _client(document_max_upload_bytes=10)
    try:
        resp = _upload(c, "big.txt", b"x" * 50)
        assert resp.status_code == 413
    finally:
        c.__exit__(None, None, None)


def test_upload_per_user_cap_409():
    c = _client(document_max_per_user=1)
    try:
        assert _upload(c, "a.txt", b"first file").status_code == 201
        assert _upload(c, "b.txt", b"second file").status_code == 409
    finally:
        c.__exit__(None, None, None)


def test_upload_unknown_analyzer_404(client):
    resp = _upload(client, "a.txt", b"data", analyzerId="does-not-exist")
    assert resp.status_code == 404


def test_upload_builtin_analyzer_accepted(client):
    resp = _upload(client, "a.txt", b"data here", analyzerId="builtin-document")
    assert resp.status_code == 201
    assert resp.json()["analyzerId"] == "builtin-document"


def test_generic_image_upload_persists_filename_derived_mime_for_mistral(
    client,
):
    client.app.state.document_ingestor._mistral = None

    response = _upload(
        client,
        "scan.png",
        b"\x89PNG\r\n\x1a\n",
        ctype="application/octet-stream",
        analyzerId="mistral-document-ai",
    )

    assert response.status_code == 201, response.text
    assert response.json()["contentType"] == "image/png"
    assert response.json()["modality"] == "image"


def test_sync_cu_analyzers_are_hidden_until_preview_is_enabled(client):
    ids = {
        analyzer["id"] for analyzer in client.get("/api/library/analyzers").json()
    }
    assert "cu-read-sync" not in ids
    assert "cu-layout-sync" not in ids
    assert "cu-agentic-document" not in ids


def test_sync_cu_upload_returns_terminal_result_in_same_request():
    client = _client(cu_preview_enabled=True)
    try:
        async def analyze_inline(_self, *_args, **_kwargs):
            from ai4ia_api.content_understanding.models import CUResult

            return CUResult(
                status="Succeeded",
                analyzer_id="prebuilt-read",
                markdown="# Read",
                contents=[{"markdown": "# Read"}],
                usage={"documentPagesBasicInline": 1},
            )

        client.app.state.document_ingestor._cu = type(
            "InlineCU", (), {"analyze_inline": analyze_inline}
        )()
        client.app.state.document_ingestor._embedder = None
        client.app.state.document_ingestor._chunks = None
        response = _upload(
            client,
            "scan.png",
            b"\x89PNG\r\n\x1a\n",
            ctype="image/png",
            analyzerId="cu-read-sync",
        )
        assert response.status_code == 201, response.text
        assert response.json()["status"] == "ready"
        assert response.json()["analysisOperation"] == "synchronous"
        assert response.json()["analysisApiVersion"] == "2026-06-01-preview"
    finally:
        client.__exit__(None, None, None)


def test_agentic_analyzer_remains_hidden_without_remote_id():
    client = _client(cu_preview_enabled=True)
    try:
        ids = {
            analyzer["id"]
            for analyzer in client.get("/api/library/analyzers").json()
        }
        assert "cu-read-sync" in ids
        assert "cu-layout-sync" in ids
        assert "cu-tax-1065-k1" in ids
        assert "cu-agentic-document" not in ids
    finally:
        client.__exit__(None, None, None)


def test_agentic_analyzer_is_advertised_only_with_configured_remote_id():
    client = _client(
        cu_preview_enabled=True,
        cu_agentic_analyzer_id="agentic.contract",
    )
    try:
        analyzers = {
            analyzer["id"]: analyzer
            for analyzer in client.get("/api/library/analyzers").json()
        }
        assert analyzers["cu-agentic-document"]["preview"] is True
        assert analyzers["cu-agentic-document"]["apiVersion"] == (
            "2026-06-01-preview"
        )
    finally:
        client.__exit__(None, None, None)


def test_sync_cu_limits_reject_oversize_and_more_than_five_pdf_pages():
    from pypdf import PdfWriter

    client = _client(cu_preview_enabled=True)
    try:
        oversized = _upload(
            client,
            "large.png",
            b"x" * (10 * 1024 * 1024 + 1),
            ctype="image/png",
            analyzerId="cu-read-sync",
        )
        writer = PdfWriter()
        for _ in range(6):
            writer.add_blank_page(width=72, height=72)
        output = io.BytesIO()
        writer.write(output)
        too_many_pages = _upload(
            client,
            "six.pdf",
            output.getvalue(),
            ctype="application/pdf",
            analyzerId="cu-layout-sync",
        )

        assert oversized.status_code == 413
        assert "10 MB" in oversized.json()["detail"]
        assert too_many_pages.status_code == 422
        assert "at most 5 pages" in too_many_pages.json()["detail"]
        assert client.get("/api/library/documents").json() == []
    finally:
        client.__exit__(None, None, None)


def test_upload_rejects_mistral_when_residency_policy_excludes_deployment():
    client = _client(data_residency="us")
    try:
        response = _upload(
            client,
            "a.pdf",
            b"%PDF-1.4",
            ctype="application/pdf",
            analyzerId="mistral-document-ai",
        )
        assert response.status_code == 422
        assert "data-residency policy" in response.json()["detail"]
        assert client.get("/api/library/documents").json() == []
    finally:
        client.__exit__(None, None, None)


def test_mistral_pdf_page_limit_allows_30_and_rejects_31_before_persistence(
    client,
):
    from pypdf import PdfWriter

    client.app.state.document_ingestor._mistral = None

    def pdf_with_pages(count: int) -> bytes:
        writer = PdfWriter()
        for _ in range(count):
            writer.add_blank_page(width=72, height=72)
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()

    allowed = _upload(
        client,
        "thirty.pdf",
        pdf_with_pages(30),
        ctype="application/pdf",
        analyzerId="mistral-document-ai",
    )
    rejected = _upload(
        client,
        "thirty-one.pdf",
        pdf_with_pages(31),
        ctype="application/pdf",
        analyzerId="mistral-document-ai",
    )

    assert allowed.status_code == 201, allowed.text
    assert rejected.status_code == 422
    assert "at most 30 pages" in rejected.json()["detail"]
    documents = client.get("/api/library/documents").json()
    assert [document["filename"] for document in documents] == ["thirty.pdf"]
