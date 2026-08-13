"""Content Understanding client: request shaping, Operation-Location
parsing, the submit→poll loop with terminal states + timeout, auth header
construction, and result normalization. The upstream is faked with
``httpx.MockTransport`` so no network is required.
"""
from __future__ import annotations

import httpx
import pytest

from ai4ia_api.config import Settings
from ai4ia_api.content_understanding.client import (
    ContentUnderstandingClient,
    ContentUnderstandingError,
)
from ai4ia_api.content_understanding.models import is_valid_analyzer_id, parse_result

_OP_URL = (
    "https://cu.example/contentunderstanding/analyzerResults/req-1"
    "?api-version=2025-11-01"
)


def _settings(**overrides) -> Settings:
    base = dict(
        env="local",
        auth_provider="dev",
        allow_dev_auth=True,
        session_store="memory",
        model_gateway_url="http://gw.test",
        cu_base_url="https://cu.example",
        cu_auth_mode="api_key",
        cu_api_key="secret-key",
        cu_poll_interval_seconds=0.0,
        cu_max_poll_seconds=5.0,
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


async def _noop_sleep(_seconds: float) -> None:
    return None


async def test_submit_binary_posts_bytes_and_returns_operation_location():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["content"] = request.content
        return httpx.Response(202, headers={"operation-location": _OP_URL})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ContentUnderstandingClient(_settings(), http_client=http)
        op = await client.submit_binary("prebuilt-documentSearch", b"PDFBYTES", "application/pdf")

    assert op == _OP_URL
    assert seen["method"] == "POST"
    assert seen["url"] == (
        "https://cu.example/contentunderstanding/analyzers/"
        "prebuilt-documentSearch:analyzeBinary?api-version=2025-11-01"
    )
    assert seen["headers"]["Ocp-Apim-Subscription-Key"] == "secret-key"
    assert seen["headers"]["content-type"] == "application/pdf"
    assert seen["content"] == b"PDFBYTES"


async def test_submit_binary_missing_operation_location_raises():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(202)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ContentUnderstandingClient(_settings(), http_client=http)
        with pytest.raises(ContentUnderstandingError):
            await client.submit_binary("a", b"x", "application/pdf")


async def test_submit_binary_upstream_error_raises_with_status():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": "Forbidden",
                    "message": "received data:image/png;base64,PRIVATE",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ContentUnderstandingClient(_settings(), http_client=http)
        with pytest.raises(ContentUnderstandingError) as ei:
            await client.submit_binary("a", b"x", "application/pdf")
    assert ei.value.status_code == 403
    assert ei.value.detail == "upstream code=Forbidden"
    assert "PRIVATE" not in str(ei.value)


async def test_bearer_auth_uses_injected_token_provider():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(202, headers={"operation-location": _OP_URL})

    async def token() -> str:
        return "tok-123"

    settings = _settings(cu_auth_mode="bearer", cu_api_key=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ContentUnderstandingClient(settings, http_client=http, token_provider=token)
        await client.submit_binary("a", b"x", "application/pdf")
    assert seen["auth"] == "Bearer tok-123"


async def test_bearer_auth_static_key_takes_precedence_over_token():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(202, headers={"operation-location": _OP_URL})

    settings = _settings(cu_auth_mode="bearer", cu_api_key="static-bearer")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ContentUnderstandingClient(settings, http_client=http)
        await client.submit_binary("a", b"x", "application/pdf")
    assert seen["auth"] == "Bearer static-bearer"


async def test_analyze_polls_until_succeeded():
    state = {"polls": 0}
    done = {
        "id": "req-1",
        "status": "Succeeded",
        "result": {
            "analyzerId": "prebuilt-documentSearch",
            "contents": [{"markdown": "# Title\n\nbody text", "fields": {"k": "v"}}],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, headers={"operation-location": _OP_URL})
        state["polls"] += 1
        if state["polls"] < 2:
            return httpx.Response(200, json={"id": "req-1", "status": "Running"})
        return httpx.Response(200, json=done)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ContentUnderstandingClient(_settings(), http_client=http)
        result = await client.analyze(
            "prebuilt-documentSearch", b"x", "application/pdf", sleep=_noop_sleep
        )

    assert result.succeeded
    assert result.markdown == "# Title\n\nbody text"
    assert result.fields == {"k": "v"}
    assert result.analyzer_id == "prebuilt-documentSearch"
    assert state["polls"] == 2


async def test_analyze_inline_returns_direct_preview_response_without_polling():
    seen: dict = {}
    body = {
        "analyzerId": "prebuilt-layout",
        "contents": [
            {
                "markdown": "# Layout",
                "fields": {
                    "signed": {
                        "type": "boolean",
                        "valueBoolean": True,
                        "confidence": 0.9,
                        "source": "D(1,0,0,1,1)",
                    }
                },
                "signatures": [{"span": {"offset": 1, "length": 4}}],
            }
        ],
        "usage": {
            "documentPagesBasic": 1,
            "contextualizationTokens": 12,
            "tokens": {"gpt-5.2-input": 10, "gpt-5.2-output": 2},
        },
        "content_filters": [{"blocked": False}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ContentUnderstandingClient(_settings(), http_client=http)
        result = await client.analyze_inline(
            "prebuilt-layout", b"PDF", "application/pdf"
        )

    assert seen == {
        "method": "POST",
        "url": (
            "https://cu.example/contentunderstanding/analyzers/"
            "prebuilt-layout:analyzeBinaryInline?"
            "api-version=2026-06-01-preview"
        ),
    }
    assert result.succeeded
    assert result.markdown == "# Layout"
    assert result.page_count == 1
    assert result.model_token_counts() == (10, 2, 12, True)
    assert result.content_filters == [{"blocked": False}]
    assert result.field_evidence_summary() == {
        "confidenceCount": 1,
        "groundedFieldCount": 1,
        "averageConfidence": 0.9,
        "minimumConfidence": 0.9,
    }


async def test_get_analyzer_returns_supported_models_for_requested_version():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith(
            "/contentunderstanding/analyzers/prebuilt-documentSearch"
            "?api-version=2026-06-01-preview"
        )
        return httpx.Response(
            200,
            json={
                "analyzerId": "prebuilt-documentSearch",
                "supportedModels": {
                    "completion": ["gpt-5.2"],
                    "embedding": ["text-embedding-3-large"],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ContentUnderstandingClient(_settings(), http_client=http)
        analyzer = await client.get_analyzer(
            "prebuilt-documentSearch",
            api_version="2026-06-01-preview",
        )

    assert analyzer["supportedModels"]["completion"] == ["gpt-5.2"]


async def test_analyze_times_out_when_never_terminal():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, headers={"operation-location": _OP_URL})
        return httpx.Response(200, json={"id": "req-1", "status": "Running"})

    settings = _settings(cu_max_poll_seconds=0.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ContentUnderstandingClient(settings, http_client=http)
        with pytest.raises(ContentUnderstandingError) as ei:
            await client.analyze("a", b"x", "application/pdf", sleep=_noop_sleep)
    assert ei.value.status_code == 408


async def test_analyze_returns_failed_result_without_raising():
    failed = {"id": "req-1", "status": "Failed", "result": {"analyzerId": "a"}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, headers={"operation-location": _OP_URL})
        return httpx.Response(200, json=failed)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ContentUnderstandingClient(_settings(), http_client=http)
        result = await client.analyze("a", b"x", "application/pdf", sleep=_noop_sleep)
    assert not result.succeeded
    assert result.status == "Failed"


def test_parse_result_concatenates_contents_and_collects_warnings():
    body = {
        "id": "r",
        "status": "Succeeded",
        "result": {
            "analyzerId": "prebuilt-documentSearch",
            "contents": [
                {"markdown": "page one"},
                {"markdown": "page two", "fields": {"a": 1}},
            ],
            "warnings": [{"code": "x"}],
        },
    }
    result = parse_result(body)
    assert result.markdown == "page one\n\npage two"
    assert result.fields == {"a": 1}
    assert result.warnings == [{"code": "x"}]
    assert result.succeeded


def test_parse_result_collects_usage_and_evidence_from_async_envelope():
    result = parse_result(
        {
            "status": "Succeeded",
            "result": {
                "analyzerId": "a",
                "contents": [
                    {
                        "markdown": "text",
                        "fields": {
                            "amount": {
                                "valueNumber": 12,
                                "confidence": 0.75,
                                "source": "D(1,0,0,1,1)",
                            },
                            "summary": {
                                "valueString": "ok",
                                "confidence": 0.5,
                            },
                        },
                    }
                ],
            },
            "usage": {
                "documentPagesStandard": 2,
                "tokens": {
                    "gpt-5.2-input": 100,
                    "gpt-5.2-output": 20,
                    "text-embedding-3-large": 10,
                },
            },
        }
    )

    assert result.page_count == 2
    assert result.page_usage_by_meter() == {
        "content-understanding-document-standard": 2
    }
    assert result.model_token_counts() == (110, 20, 130, True)
    assert result.token_usage_by_model() == {
        "gpt-5.2": (100, 20),
        "text-embedding-3-large": (10, 0),
    }
    assert result.field_evidence_summary() == {
        "confidenceCount": 2,
        "groundedFieldCount": 1,
        "averageConfidence": 0.625,
        "minimumConfidence": 0.5,
    }


def test_parse_result_handles_missing_result():
    result = parse_result({"id": "r", "status": "Running"})
    assert result.markdown == ""
    assert result.fields == {}
    assert not result.succeeded


@pytest.mark.parametrize(
    "value",
    [
        "prebuilt-documentSearch",
        "a",
        "a" * 64,
        "with.dots.allowed",
        "-leading-hyphen",
        "_leading_underscore",
        ".leading-dot",
    ],
)
def test_is_valid_analyzer_id_accepts_contract_charset(value):
    assert is_valid_analyzer_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a" * 65,
        "foo/bar",
        "foo?x=1",
        "foo#frag",
        "foo bar",
        "foo\n",
        "foo\r\n",
        "../secrets",
    ],
)
def test_is_valid_analyzer_id_rejects_invalid_values(value):
    # "foo\n" is the key regression case: a validator built on ``match(...) +
    # "$"`` instead of ``fullmatch`` would incorrectly accept this, because
    # "$" alone matches just before a trailing newline.
    assert not is_valid_analyzer_id(value)


def test_submit_url_builds_expected_path_for_valid_analyzer_id():
    settings = _settings()
    client = ContentUnderstandingClient(settings)
    assert client.submit_url("prebuilt-documentSearch") == (
        "https://cu.example/contentunderstanding/analyzers/"
        "prebuilt-documentSearch:analyzeBinary?api-version=2025-11-01"
    )


def test_submit_url_rejects_invalid_analyzer_id():
    # Defense in depth: even if a caller bypasses the request-model validator
    # (e.g. a persisted/legacy Analyzer.baseAnalyzerId, which has no validator
    # of its own), the client must not build a request URL from it.
    settings = _settings()
    client = ContentUnderstandingClient(settings)
    with pytest.raises(ValueError, match="invalid content understanding analyzer id"):
        client.submit_url("../secrets")


async def test_submit_binary_rejects_invalid_analyzer_id_before_any_request():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP request should be made for an invalid analyzer id")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ContentUnderstandingClient(_settings(), http_client=http)
        with pytest.raises(ValueError, match="invalid content understanding analyzer id"):
            await client.submit_binary("foo\n", b"x", "application/pdf")
