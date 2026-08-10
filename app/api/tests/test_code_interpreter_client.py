"""Responses API Code Interpreter client.

All IO is injected (a fake ``httpx``-like async client + a fake token provider),
so these exercise URL building, auth-header construction for each mode, the
defensive response parser, and error mapping — with no network and no azure SDK.
"""
from __future__ import annotations

import httpx
import pytest

from ai4ia_api.code_interpreter.client import (
    CODE_INTERPRETER_TOOL,
    CodeInterpreterClient,
    CodeInterpreterError,
    code_interpreter_tool,
)
from ai4ia_api.code_interpreter.models import parse_response
from ai4ia_api.config import GatewayAuthMode
from tests.conftest import make_settings


class FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeAsyncClient:
    """Captures the single POST/DELETE and returns a canned response."""

    def __init__(self, response: FakeResponse | None = None, raise_exc: Exception | None = None) -> None:
        self._response = response
        self._raise = raise_exc
        self.calls: list[dict] = []
        self.deletes: list[dict] = []
        self.closed = False

    async def post(self, url, headers=None, json=None, files=None, data=None):
        self.calls.append(
            {"url": url, "headers": headers, "json": json, "files": files, "data": data}
        )
        if self._raise is not None:
            raise self._raise
        return self._response

    async def delete(self, url, headers=None):
        self.deletes.append({"url": url, "headers": headers})
        if self._raise is not None:
            raise self._raise
        return self._response

    async def aclose(self):
        self.closed = True


def _settings(**overrides):
    base = dict(
        document_understanding_enabled=True,
        document_compute_enabled=True,
        code_interpreter_base_url="https://res.openai.azure.com",
        code_interpreter_model="gpt-4.1",
    )
    base.update(overrides)
    return make_settings(**base)


def _client(settings, http_client, token_provider=None):
    # Default to a harmless fake AAD token so bearer-mode tests that don't care
    # about auth never reach DefaultAzureCredential (which fails in CI, where no
    # Azure identity is configured). Tests that assert a specific token pass their own.
    async def _fake_token() -> str:
        return "fake-aad-token"

    return CodeInterpreterClient(
        settings,
        http_client=http_client,
        token_provider=token_provider if token_provider is not None else _fake_token,
    )


# --- URL building ---
def test_responses_url_omits_api_version_by_default():
    c = _client(_settings(), FakeAsyncClient())
    assert c.responses_url() == "https://res.openai.azure.com/openai/v1/responses"


def test_responses_url_appends_api_version_when_set():
    c = _client(_settings(code_interpreter_api_version="preview"), FakeAsyncClient())
    assert c.responses_url().endswith("/openai/v1/responses?api-version=preview")


def test_base_url_trailing_slash_is_normalized():
    c = _client(_settings(code_interpreter_base_url="https://res.openai.azure.com/"), FakeAsyncClient())
    assert c.responses_url() == "https://res.openai.azure.com/openai/v1/responses"


# --- auth headers ---
async def test_api_key_mode_sends_api_key_header():
    settings = _settings(
        code_interpreter_auth_mode=GatewayAuthMode.api_key,
        code_interpreter_api_key="secret-key",
    )
    fake = FakeAsyncClient(FakeResponse(200, {"status": "completed", "output_text": "ok"}))
    c = _client(settings, fake)
    await c.run(instructions="do it", user_input="data")
    headers = fake.calls[0]["headers"]
    assert headers["api-key"] == "secret-key"
    assert "Authorization" not in headers


async def test_bearer_mode_with_static_key_sends_bearer():
    settings = _settings(
        code_interpreter_auth_mode=GatewayAuthMode.bearer,
        code_interpreter_api_key="static-bearer",
    )
    fake = FakeAsyncClient(FakeResponse(200, {"status": "completed", "output_text": "ok"}))
    c = _client(settings, fake)
    await c.run(instructions="i", user_input="u")
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer static-bearer"


async def test_bearer_mode_without_key_uses_aad_token_provider():
    settings = _settings(code_interpreter_auth_mode=GatewayAuthMode.bearer)
    fake = FakeAsyncClient(FakeResponse(200, {"status": "completed", "output_text": "ok"}))

    async def token_provider() -> str:
        return "aad-token-123"

    c = _client(settings, fake, token_provider=token_provider)
    await c.run(instructions="i", user_input="u")
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer aad-token-123"


# --- payload shape ---
async def test_run_posts_code_interpreter_tool_payload():
    fake = FakeAsyncClient(FakeResponse(200, {"status": "completed", "output_text": "42"}))
    c = _client(_settings(), fake)
    result = await c.run(instructions="be careful", user_input="sum it")
    body = fake.calls[0]["json"]
    assert body["model"] == "gpt-4.1"
    assert body["tools"] == [CODE_INTERPRETER_TOOL]
    assert body["instructions"] == "be careful"
    assert body["input"] == "sum it"
    assert result.output_text == "42"
    assert result.succeeded is True


async def test_run_opts_out_of_provider_side_storage():
    """This client is the documented direct-to-Foundry exception, so the
    Responses gateway's ``store: false`` does not cover it. Without an explicit
    opt-out here, ``store`` defaults to TRUE on this surface and every compute
    turn leaves the user's instructions, input and output retrievable from
    ``GET /responses/{id}`` for 30 days -- a second, ungoverned copy of user
    content that the rest of the app is careful to avoid.
    """
    fake = FakeAsyncClient(FakeResponse(200, {"status": "completed", "output_text": "42"}))
    c = _client(_settings(), fake)
    await c.run(instructions="be careful", user_input="sum it", file_ids=["file-1"])
    assert fake.calls[0]["json"]["store"] is False


# --- code_interpreter_tool factory ---
def test_code_interpreter_tool_default_is_auto_container():
    assert code_interpreter_tool() == CODE_INTERPRETER_TOOL
    assert code_interpreter_tool(None) == CODE_INTERPRETER_TOOL
    assert code_interpreter_tool([]) == CODE_INTERPRETER_TOOL


def test_code_interpreter_tool_seeds_file_ids():
    tool = code_interpreter_tool(["file-1", "file-2"])
    assert tool["type"] == "code_interpreter"
    assert tool["container"] == {"type": "auto", "file_ids": ["file-1", "file-2"]}
    # Must not mutate the shared default constant.
    assert "file_ids" not in CODE_INTERPRETER_TOOL["container"]


# --- files URL building ---
def test_files_url_collection_and_single():
    c = _client(_settings(), FakeAsyncClient())
    assert c.files_url() == "https://res.openai.azure.com/openai/v1/files"
    assert c.files_url("file-9") == "https://res.openai.azure.com/openai/v1/files/file-9"


def test_files_url_appends_api_version():
    c = _client(_settings(code_interpreter_api_version="preview"), FakeAsyncClient())
    assert c.files_url().endswith("/openai/v1/files?api-version=preview")
    assert c.files_url("file-9").endswith("/openai/v1/files/file-9?api-version=preview")


# --- run with file_ids ---
async def test_run_with_file_ids_seeds_container():
    fake = FakeAsyncClient(FakeResponse(200, {"status": "completed", "output_text": "ok"}))
    c = _client(_settings(), fake)
    await c.run(instructions="i", user_input="u", file_ids=["file-7"])
    body = fake.calls[0]["json"]
    assert body["tools"] == [
        {"type": "code_interpreter", "container": {"type": "auto", "file_ids": ["file-7"]}}
    ]


# --- upload_file ---
async def test_upload_file_posts_multipart_and_returns_id():
    fake = FakeAsyncClient(FakeResponse(200, {"id": "file-uploaded-1"}))
    c = _client(_settings(), fake)
    file_id = await c.upload_file(
        filename="report.csv", content=b"a,b\n1,2\n", content_type="text/csv"
    )
    assert file_id == "file-uploaded-1"
    call = fake.calls[0]
    assert call["url"] == "https://res.openai.azure.com/openai/v1/files"
    # Multipart upload must NOT carry a JSON content-type (httpx sets the boundary).
    assert "Content-Type" not in call["headers"]
    assert call["json"] is None
    assert call["data"] == {"purpose": "assistants"}
    assert call["files"]["file"] == ("report.csv", b"a,b\n1,2\n", "text/csv")


async def test_upload_file_defaults_content_type_and_filename():
    fake = FakeAsyncClient(FakeResponse(200, {"id": "file-2"}))
    c = _client(_settings(), fake)
    await c.upload_file(filename="", content=b"x")
    assert fake.calls[0]["files"]["file"] == ("file", b"x", "application/octet-stream")


async def test_upload_file_missing_id_raises():
    fake = FakeAsyncClient(FakeResponse(200, {"object": "file"}))
    c = _client(_settings(), fake)
    with pytest.raises(CodeInterpreterError):
        await c.upload_file(filename="f.csv", content=b"x")


async def test_upload_file_non_2xx_raises():
    fake = FakeAsyncClient(FakeResponse(413, None, text="too large"))
    c = _client(_settings(), fake)
    with pytest.raises(CodeInterpreterError) as ei:
        await c.upload_file(filename="f.csv", content=b"x")
    assert ei.value.status_code == 413


async def test_upload_file_transport_error_raises():
    fake = FakeAsyncClient(raise_exc=httpx.ConnectError("boom"))
    c = _client(_settings(), fake)
    with pytest.raises(CodeInterpreterError):
        await c.upload_file(filename="f.csv", content=b"x")


# --- delete_file (best-effort, never raises) ---
async def test_delete_file_issues_delete():
    fake = FakeAsyncClient(FakeResponse(200, {"deleted": True}))
    c = _client(_settings(), fake)
    assert await c.delete_file("file-9") is True
    assert fake.deletes[0]["url"] == "https://res.openai.azure.com/openai/v1/files/file-9"


async def test_delete_file_swallows_errors():
    fake = FakeAsyncClient(raise_exc=httpx.ConnectError("boom"))
    c = _client(_settings(), fake)
    assert await c.delete_file("file-9") is False


async def test_delete_file_empty_id_is_noop():
    fake = FakeAsyncClient(FakeResponse(200, {"deleted": True}))
    c = _client(_settings(), fake)
    assert await c.delete_file("") is False
    assert fake.deletes == []


# --- error mapping ---
async def test_non_2xx_raises_code_interpreter_error():
    fake = FakeAsyncClient(FakeResponse(429, None, text="rate limited"))
    c = _client(_settings(), fake)
    with pytest.raises(CodeInterpreterError) as ei:
        await c.run(instructions="i", user_input="u")
    assert ei.value.status_code == 429


async def test_transport_error_raises_code_interpreter_error():
    fake = FakeAsyncClient(raise_exc=httpx.ConnectError("boom"))
    c = _client(_settings(), fake)
    with pytest.raises(CodeInterpreterError):
        await c.run(instructions="i", user_input="u")


async def test_non_json_body_raises():
    fake = FakeAsyncClient(FakeResponse(200, None, text="not json"))
    c = _client(_settings(), fake)
    with pytest.raises(CodeInterpreterError):
        await c.run(instructions="i", user_input="u")


@pytest.mark.parametrize("status", ["failed", "incomplete"])
async def test_http_200_non_success_status_raises_typed_error(status: str):
    fake = FakeAsyncClient(
        FakeResponse(200, {"status": status, "output_text": ""})
    )
    c = _client(_settings(), fake)

    with pytest.raises(CodeInterpreterError) as exc:
        await c.run(instructions="i", user_input="u")

    assert exc.value.status_code == 200
    assert f"status={status}" in exc.value.detail


async def test_http_200_malformed_status_raises_typed_error():
    fake = FakeAsyncClient(FakeResponse(200, {"output_text": "looks successful"}))
    c = _client(_settings(), fake)

    with pytest.raises(CodeInterpreterError) as exc:
        await c.run(instructions="i", user_input="u")

    assert exc.value.status_code == 200
    assert "status=missing" in exc.value.detail


@pytest.mark.parametrize("status", ["completed", "succeeded"])
async def test_http_200_success_status_returns_result(status: str):
    fake = FakeAsyncClient(
        FakeResponse(200, {"status": status, "output_text": "ok"})
    )
    c = _client(_settings(), fake)

    result = await c.run(instructions="i", user_input="u")

    assert result.succeeded is True
    assert result.output_text == "ok"


# --- response parser ---
def test_parse_top_level_output_text():
    r = parse_response({"status": "completed", "output_text": "the answer is 7"})
    assert r.output_text == "the answer is 7"
    assert r.logs == []
    assert r.artifacts == []


def test_parse_falls_back_to_message_items():
    body = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "fallback answer"}],
            }
        ],
    }
    assert parse_response(body).output_text == "fallback answer"


def test_parse_collects_ci_logs_and_artifacts():
    body = {
        "status": "completed",
        "output_text": "done",
        "output": [
            {
                "type": "code_interpreter_call",
                "code": "print('hi')",
                "outputs": [
                    {"type": "logs", "logs": "hi\n"},
                    {"type": "image", "file_id": "img-1"},
                ],
            }
        ],
    }
    r = parse_response(body)
    assert r.logs == ["hi\n"]
    assert r.artifacts == ["img-1"]


def test_parse_degrades_on_unknown_shape():
    r = parse_response({"weird": True})
    assert r.output_text == ""
    assert r.succeeded is False
