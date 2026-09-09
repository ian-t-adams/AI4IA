"""Run real API/adapter/orchestration seams against isolated synthetic providers."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import sys
import threading
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Protocol as TypingProtocol, TypeVar
from unittest.mock import patch

import httpx
from pydantic_settings.sources import DotEnvSettingsSource

from .contracts import (
    ROOT, Case, CaseResult, Dataset, EvaluationError, ModelIdentity, Protocol, bounded_json,
)
from .oracles import Observation, score_case

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

GATEWAY_FIXTURE_VERSION = "azure-native-offline-http-1"
GATEWAY_URL = "https://gateway.invalid"
_COURIER = "mcp_courier_send"
_OWNER = {"X-Dev-User": "synthetic-evaluation-owner"}
_OTHER = {"X-Dev-User": "synthetic-evaluation-other"}


@dataclass
class NetworkGuard:
    attempts: int = 0

    def deny(self, *_args, **_kwargs):
        self.attempts += 1
        raise EvaluationError("offline_network_denied")

    async def deny_async(self, *_args, **_kwargs):
        self.deny()


@contextmanager
def offline_environment() -> Iterator[NetworkGuard]:
    """No ambient app config, credentials, proxies, exporters, or real transports.

    Windows asyncio may implement its wakeup socketpair with loopback TCP. Only
    the stdlib socketpair construction gets that exception, on its own thread;
    arbitrary loopback connections and every other network connect remain denied.
    """
    guard = NetworkGuard()
    pair_state = threading.local()
    original_pair = socket.socketpair
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_lookup = socket.getaddrinfo

    def pair(*args, **kwargs):
        pair_state.active = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            pair_state.active = False

    def local_pair(address) -> bool:
        return bool(
            getattr(pair_state, "active", False) and isinstance(address, tuple)
            and address[0] in ("127.0.0.1", "::1")
        )

    def connect(connection, address):
        if local_pair(address):
            return original_connect(connection, address)
        return guard.deny()

    def connect_ex(connection, address):
        if local_pair(address):
            return original_connect_ex(connection, address)
        return guard.deny()

    def lookup(host, *args, **kwargs):
        if getattr(pair_state, "active", False) and host in ("127.0.0.1", "::1"):
            return original_lookup(host, *args, **kwargs)
        return guard.deny()

    # SystemRoot is needed by Windows' loader, not a config/credential source.
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    old_path = list(sys.path)
    old_logging = logging.root.manager.disable
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, environment, clear=True))
        # main's import-time app is constructed before make_settings can supply
        # _env_file=None. Prevent even reading dotenv before any app/helper import.
        stack.enter_context(patch.object(DotEnvSettingsSource, "_read_env_files", return_value={}))
        stack.enter_context(patch.object(socket, "socketpair", pair))
        stack.enter_context(patch.object(socket.socket, "connect", connect))
        stack.enter_context(patch.object(socket.socket, "connect_ex", connect_ex))
        stack.enter_context(patch.object(socket, "getaddrinfo", lookup))
        stack.enter_context(patch.object(socket, "create_connection", guard.deny))
        stack.enter_context(patch.object(httpx.HTTPTransport, "handle_request", guard.deny))
        stack.enter_context(patch.object(
            httpx.AsyncHTTPTransport, "handle_async_request", guard.deny_async,
        ))
        sys.path[:0] = [str(ROOT / "app" / "api" / "src"), str(ROOT / "app" / "api")]
        logging.disable(logging.CRITICAL)
        try:
            yield guard
        finally:
            sys.path[:] = old_path
            logging.disable(old_logging)


@dataclass(frozen=True)
class ResolvedModel:
    identity: ModelIdentity
    deployment: str


def resolve_models(dataset: Dataset) -> dict[Protocol, ResolvedModel]:
    from ai4ia_api.catalog import load_catalog

    raw = bounded_json(ROOT / "infra" / "models.json", 262_144, "catalog_too_large")
    if not isinstance(raw, dict):
        raise EvaluationError("invalid_model_catalog")
    catalog = load_catalog(None, "global", True)
    resolved: dict[Protocol, ResolvedModel] = {}
    for protocol, fixture_version in sorted(dataset.provider_versions.items()):
        candidates = sorted(
            (
                model for model in catalog.conversational_models()
                if model.api == protocol and model.supportsTools
            ),
            key=lambda model: model.id,
        )
        if not candidates:
            raise EvaluationError("unidentified_model")
        model = candidates[0]
        option = catalog.resolve_deployment(model.id)
        if option is None:
            raise EvaluationError("unidentified_deployment")
        source = next((item for item in raw["catalog"] if item["name"] == model.id), None)
        versions = {
            item["version"] for item in (source or {}).get("deployments", [])
            if item["region"] == option.region and item["sku"] == option.sku
        }
        if len(versions) != 1:
            raise EvaluationError("unidentified_model_version")
        resolved[protocol] = ResolvedModel(
            identity=ModelIdentity(
                protocol=protocol, model_id=model.id, catalog_model_version=versions.pop(),
                provider_fixture_version=fixture_version,
            ),
            deployment=option.deploymentName,
        )
    return resolved


def settings_for(case: Case):
    from app.api.tests.conftest import make_settings

    return make_settings(
        model_gateway_url=GATEWAY_URL,
        claude_enabled=True,
        custom_tools_enabled=(
            case.scenario == "approval" or
            (case.scenario == "workflow" and case.expected.status == "error")
        ),
        document_understanding_enabled=case.scenario == "document",
        user_directory_enabled=False,
        entitlements_enabled=False,
        usage_metering_enabled=True,
        applicationinsights_connection_string=None,
    )


class NativeScript:
    """Only fixture bytes are mocked; the real gateway builds and decodes HTTP."""

    def __init__(self, case: Case, observed: Observation) -> None:
        self.case, self.observed = case, observed

    def _materialize(self, value):
        if isinstance(value, dict):
            return {key: self._materialize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._materialize(item) for item in value]
        if isinstance(value, str):
            return self.observed.aliases.get(value, value)
        return value

    def handle(self, request: httpx.Request) -> httpx.Response:
        from app.api.tests.conftest import sse_chunks

        observed = self.observed
        if (
            request.url.host != "gateway.invalid" or request.url.scheme != "https"
            or request.method != "POST" or len(request.content) > 131_072
            or "authorization" in request.headers or "ocp-apim-subscription-key" in request.headers
        ):
            observed.transport_valid = False
            raise EvaluationError("unexpected_fixture_request")
        body = json.loads(request.content)
        observed.requests.append(body)
        observed.request_paths.append(request.url.path)
        expected_path = (
            "/responses" if self.case.protocol == "responses"
            else f"/deployments/{observed.deployment}/chat/completions"
        )
        valid = request.url.path == expected_path
        if self.case.protocol == "responses":
            valid = valid and body.get("store") is False and body.get("model") == observed.deployment
        elif self.case.protocol == "anthropic":
            valid = valid and body.get("model") == observed.deployment and "max_tokens" in body
        observed.transport_valid = observed.transport_valid and valid
        index = observed.consumed_replies
        if index >= len(self.case.replies):
            raise EvaluationError("fixture_exhausted")
        reply = self.case.replies[index]
        observed.consumed_replies += 1
        observed.fixture_latency_ms += reply.latency_ms
        payload = self._materialize(deepcopy(reply.body))
        if not isinstance(payload, dict):
            raise EvaluationError("invalid_fixture_body")
        if body.get("stream"):
            if self.case.protocol != "chat":
                raise EvaluationError("unsupported_stream_fixture")
            wire = "".join(f"data: {chunk.raw}\n\n" for chunk in sse_chunks(payload))
            return httpx.Response(200, content=wire, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=payload)


class StatusResponse(TypingProtocol):
    @property
    def status_code(self) -> int: ...


ResponseT = TypeVar("ResponseT", bound=StatusResponse)


def _success(response: ResponseT, status: int = 200) -> ResponseT:
    if response.status_code != status:
        raise EvaluationError("unexpected_api_status")
    return response


def _session(client: TestClient, model_id: str, headers: dict) -> str:
    return _success(client.post(
        "/api/sessions", json={"title": "Synthetic evaluation", "model": model_id}, headers=headers,
    ), 201).json()["id"]


def _agent(client: TestClient, case: Case, name: str, tools: list[str], headers: dict) -> None:
    _success(client.post("/api/agents", json={
        "name": name, "systemPrompt": case.system_prompt, "tools": tools,
    }, headers=headers), 201)


def _courier(client: TestClient, case: Case, headers: dict) -> None:
    _success(client.post("/api/agents/mcp-servers", json={
        "name": "courier", "endpoint": "https://courier.example.test/rpc", "trusted": True,
    }, headers=headers), 201)
    _agent(client, case, "evalcourier", ["mcp:courier/send"], headers)


def _assistant(client: TestClient, session_id: str, headers: dict) -> dict:
    rows = _success(client.get(
        f"/api/sessions/{session_id}/messages", headers=headers,
    )).json()
    assistants = [row for row in rows if row["role"] == "assistant"]
    if not assistants:
        raise EvaluationError("missing_assistant")
    return assistants[-1]


def _turn(
    client: TestClient, case: Case, session_id: str, headers: dict,
    observed: Observation, *, agent: str | None = None, decisions: list[dict] | None = None,
) -> dict:
    response = _success(client.post("/api/chat", json={
        "sessionId": session_id,
        "content": f"@{agent} {case.input}" if agent else case.input,
        "stream": case.stream, "approvals": decisions or [],
    }, headers=headers))
    if case.stream:
        observed.stream_finished = observed.stream_finished and "[DONE]" in response.text
        payload = {}
    else:
        payload = response.json()
    observed.messages.append(_assistant(client, session_id, headers))
    return payload


class FixtureEmbedder:
    def __init__(self, dimensions: int) -> None:
        self.vector = [1.0] + [0.0] * (dimensions - 1)

    async def embed_one(self, _text):
        return list(self.vector)

    async def embed(self, inputs):
        return [list(self.vector) for _ in inputs]


async def _seed_documents(
    app: FastAPI, case: Case, observed: Observation, owner_id: str, other_id: str,
) -> None:
    from ai4ia_api.library.blob_store import PARSED_NAME, blob_path
    from ai4ia_api.library.doc_chunks import DocChunkRecord
    from ai4ia_api.library.models import DocumentStatus, UserDocument

    fixture = case.document
    if fixture is None:
        raise EvaluationError("missing_document_fixture")
    ingestor = app.state.document_ingestor
    settings = app.state.settings
    embedder = FixtureEmbedder(settings.memory_embedding_dimensions)
    # Foreign and owned chunks are deliberately equally similar.
    for user_id, text, owned in (
        (owner_id, fixture.text, True),
        (other_id, fixture.foreign_text, False),
    ):
        version = hashlib.sha256(text.encode("utf-8")).hexdigest()
        doc = UserDocument(
            userId=user_id, filename=fixture.filename, contentHash=version,
            status=DocumentStatus.ready, summary="Synthetic source",
        )
        path = blob_path(user_id, doc.id, PARSED_NAME)
        await ingestor.blob.put(path, text.encode("utf-8"), "text/markdown")
        doc.parsedPath = path
        await ingestor.library.create_document(doc)
        await ingestor.chunks.add_many(
            [DocChunkRecord(
                user_id=user_id, document_id=doc.id, chunk_index=0, content=text,
                heading="Timeline", char_start=0, char_end=len(text),
            )],
            [embedder.vector],
        )
        if owned:
            observed.owned_sources[doc.id] = {
                "text": text, "version": version, "filename": fixture.filename,
            }
        else:
            observed.foreign_sources.add(doc.id)


def _approvals(
    client: TestClient, case: Case, session_id: str, observed: Observation, connector,
) -> None:
    first = _turn(client, case, session_id, _OWNER, observed, agent="evalcourier")
    prompts = first.get("approvals") or []
    observed.approval_prompts.append(len(prompts))
    observed.dispatch_counts.append(len(connector.tool_calls))
    decision = [{"requestId": prompts[0]["id"], "grant": prompts[0]["grant"]}] if prompts else []
    original_id = observed.messages[0]["id"]
    headers, target_session = _OWNER, session_id
    if case.approval_variant == "cross-owner":
        headers = _OTHER
        _courier(client, case, headers)
        observed.owner_read_denied = client.get(
            f"/api/sessions/{session_id}/messages", headers=headers,
        ).status_code == 404
    if case.approval_variant in ("cross-owner", "cross-session"):
        target_session = _session(client, observed.model_id, headers)
    for _ in range(2 if case.approval_variant == "exact-replay" else 1):
        before = len(connector.tool_calls)
        reply = _turn(
            client, case, target_session, headers, observed, agent="evalcourier", decisions=decision,
        )
        observed.approval_prompts.append(len(reply.get("approvals") or []))
        observed.dispatch_counts.append(len(connector.tool_calls) - before)
    initial_args = json.loads(
        case.replies[0].body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    )
    observed.exact_dispatch = all(call[2] == initial_args for call in connector.tool_calls)
    original = next(row for row in _success(client.get(
        f"/api/sessions/{session_id}/messages", headers=_OWNER,
    )).json() if row["id"] == original_id)
    records = original.get("pendingApprovals") or []
    observed.original_approval_consumed = records[0]["consumed"] if records else None


def execute_case(dataset: Dataset, case: Case) -> CaseResult:
    with offline_environment() as guard:
        import ai4ia_api
        from ai4ia_api import main
        from ai4ia_api.agents.mcp_client import FakeMcpConnector, McpToolResult
        from ai4ia_api.agents.mcp_secrets import InMemoryMcpSecretStore
        from ai4ia_api.agents.mcp_servers import DiscoveredTool, tool_alias
        from ai4ia_api.agents.mcp_service import McpServerService
        from ai4ia_api.agents.mcp_store import InMemoryUserMcpServerStore
        from ai4ia_api.usage.pricing import PriceRate, PricingBook
        from fastapi.testclient import TestClient

        if ROOT / "app" / "api" / "src" not in Path(ai4ia_api.__file__).parents:
            raise EvaluationError("wrong_application_checkout")
        resolved = resolve_models(dataset)[case.protocol]
        observed = Observation(
            model_id=resolved.identity.model_id, deployment=resolved.deployment,
        )
        settings = settings_for(case)
        connector = FakeMcpConnector(
            [DiscoveredTool(
                name="send", description="Send a synthetic message",
                inputSchema={
                    "type": "object", "required": ["to", "body"],
                    "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
                },
            )],
            call_results={"send": McpToolResult(content="synthetic-delivery")},
        )
        if settings.custom_tools_enabled:
            observed.aliases[_COURIER] = tool_alias("courier", "send")
        prices = PricingBook(
            {observed.model_id: PriceRate(
                dataset.config.input_per_1m, dataset.config.output_per_1m,
            )} if dataset.config.pricing_enabled else {},
            currency="USD", version=dataset.config.price_version,
        )
        script = NativeScript(case, observed)
        http_factory = partial(
            httpx.AsyncClient, transport=httpx.MockTransport(script.handle), trust_env=False,
        )
        with patch.object(main.httpx, "AsyncClient", http_factory), patch.object(
            main, "load_pricing", return_value=prices,
        ):
            app = main.create_app(settings)
            with TestClient(app) as client:
                if settings.custom_tools_enabled:
                    app.state.mcp_service = McpServerService(
                        InMemoryUserMcpServerStore(), connector=connector,
                        secret_store=InMemoryMcpSecretStore(),
                        resolver=lambda _host: ["93.184.216.34"],
                    )
                    _courier(client, case, _OWNER)
                session_id = _session(client, observed.model_id, _OWNER)
                if case.scenario == "agent":
                    _agent(client, case, "evalcalc", ["calculator"], _OWNER)
                    _turn(client, case, session_id, _OWNER, observed, agent="evalcalc")
                elif case.scenario == "workflow":
                    if not settings.custom_tools_enabled:
                        _agent(client, case, "evalcalc", ["calculator"], _OWNER)
                        _agent(client, case, "evalwriter", [], _OWNER)
                    steps = [{
                        "agent": "evalcourier" if settings.custom_tools_enabled else
                                 "evalcalc" if index == 0 else "evalwriter",
                        "instruction": instruction,
                    } for index, instruction in enumerate(case.workflow_instructions)]
                    _success(client.post("/api/workflows", json={
                        "name": "evalflow", "steps": steps,
                    }, headers=_OWNER), 201)
                    _success(client.post("/api/workflows/evalflow/run", json={
                        "sessionId": session_id, "input": case.input, "autoApproveTools": False,
                    }, headers=_OWNER))
                    observed.messages.append(_assistant(client, session_id, _OWNER))
                    if settings.custom_tools_enabled:
                        observed.dispatch_counts.append(len(connector.tool_calls))
                elif case.scenario == "approval":
                    _approvals(client, case, session_id, observed, connector)
                else:
                    if case.scenario == "document":
                        owners = [
                            _success(client.get(
                                "/api/entitlement", headers=headers,
                            )).json()["userId"] for headers in (_OWNER, _OTHER)
                        ]
                        if client.portal is None:
                            raise EvaluationError("missing_test_portal")
                        client.portal.call(
                            _seed_documents, app, case, observed, owners[0], owners[1],
                        )
                        if app.state.document_ingestor.embedder is None:
                            raise EvaluationError("missing_embedding_configuration")
                        fixture_embedder = FixtureEmbedder(settings.memory_embedding_dimensions)
                        with patch.object(
                            app.state.document_ingestor.embedder, "embed",
                            side_effect=fixture_embedder.embed,
                        ):
                            _turn(client, case, session_id, _OWNER, observed)
                    else:
                        _turn(client, case, session_id, _OWNER, observed)
        observed.network_attempts = guard.attempts
        return score_case(dataset, case, observed)
