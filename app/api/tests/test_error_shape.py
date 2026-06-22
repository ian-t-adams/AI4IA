"""Every API error returns one consistent JSON body.

The shared shape is ``{"detail": str, "code": str, "correlation_id": str}``
regardless of whether the error comes from an ``HTTPException`` or from
request-body validation. Validation failures keep ``detail`` a plain string and
move the field-level detail under ``errors`` so the shape never varies.
"""
from __future__ import annotations

CORRELATION_HEADER = "x-correlation-id"


def _assert_error_shape(body: dict) -> None:
    assert isinstance(body["detail"], str)
    assert isinstance(body["code"], str) and body["code"]
    assert isinstance(body["correlation_id"], str) and body["correlation_id"]


def test_http_exception_404_has_consistent_shape(client):
    resp = client.get("/api/sessions/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    _assert_error_shape(body)
    assert body["detail"] == "Session not found"
    assert body["code"] == "not_found"


def test_artifact_404_has_consistent_shape(client):
    # Bad id fails the artifact-id regex before any blob lookup -> 404.
    resp = client.get("/api/documents/artifacts/not-a-real-id")
    assert resp.status_code == 404
    body = resp.json()
    _assert_error_shape(body)
    assert body["detail"] == "Not found."
    assert body["code"] == "not_found"


def test_validation_error_422_has_consistent_shape(client):
    # Missing the required ``input`` field -> RequestValidationError.
    resp = client.post("/api/voice/speech", json={})
    assert resp.status_code == 422
    body = resp.json()
    _assert_error_shape(body)
    assert body["detail"] == "Request validation failed."
    assert body["code"] == "validation_error"
    assert isinstance(body["errors"], list) and body["errors"]


def test_correlation_id_echoes_request_header(client):
    cid = "shape-test-correlation-id"
    resp = client.get(
        "/api/documents/artifacts/not-a-real-id",
        headers={CORRELATION_HEADER: cid},
    )
    assert resp.status_code == 404
    assert resp.headers[CORRELATION_HEADER] == cid
    assert resp.json()["correlation_id"] == cid
