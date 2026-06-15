"""MCP client adapter (Phase 12A) — tool *discovery* over Streamable HTTP.

We speak the Model Context Protocol's JSON-RPC handshake directly over
``httpx`` (already a dependency) rather than pulling in a heavier SDK: the
discovery surface we need is small and pinning it here keeps the egress path
fully under our control (no redirects, bounded time + size, explicit headers).

The connector is a narrow seam (:class:`McpConnector`) so the service depends on
the capability, not the transport: tests use :class:`FakeMcpConnector`, and the
live :class:`HttpxMcpConnector`'s framing/auth/error handling is unit-tested
with ``httpx.MockTransport`` (no live server required).

Discovery performs the minimal MCP flow: ``initialize`` → ``notifications/
initialized`` → ``tools/list``. Execution of a discovered tool per chat turn is
a later sub-phase; this module only lists what a server offers.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .mcp_servers import (
    MAX_TOOL_DESCRIPTION_LEN,
    MAX_TOOL_NAME_LEN,
    MAX_TOOLS_PER_SERVER,
    DiscoveredTool,
    McpAuthMode,
    McpConnectionError,
)

# The MCP protocol revision we advertise. Servers negotiate down if needed; this
# is sent both in the initialize params and as a header on later requests.
PROTOCOL_VERSION = "2025-06-18"

_CLIENT_INFO = {"name": "ai4ia", "version": "1.0"}
_DEFAULT_TIMEOUT_S = 15.0
_DEFAULT_MAX_BYTES = 2_000_000


@dataclass(frozen=True)
class McpAuth:
    """A transient credential for one discovery call (never persisted)."""

    mode: McpAuthMode = McpAuthMode.none
    secret: str | None = None

    def headers(self) -> dict[str, str]:
        if self.mode is McpAuthMode.bearer and self.secret:
            return {"Authorization": f"Bearer {self.secret}"}
        if self.mode is McpAuthMode.api_key and self.secret:
            return {"X-API-Key": self.secret}
        return {}


class McpConnector(Protocol):
    async def discover(self, *, endpoint: str, auth: McpAuth) -> list[DiscoveredTool]: ...


class HttpxMcpConnector:
    """Live MCP Streamable-HTTP connector.

    ``client`` may be injected (tests pass one backed by ``httpx.MockTransport``);
    otherwise a short-lived client is created per call with redirects disabled
    (a redirect could otherwise bounce an already-validated host to an internal
    one — SSRF defense in depth on top of the URL guard).
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        self._client = client
        self._timeout_s = timeout_s
        self._max_bytes = max_bytes

    async def discover(self, *, endpoint: str, auth: McpAuth) -> list[DiscoveredTool]:
        if self._client is not None:
            return await self._discover_with(self._client, endpoint, auth)
        async with httpx.AsyncClient(
            timeout=self._timeout_s, follow_redirects=False
        ) as client:
            return await self._discover_with(client, endpoint, auth)

    async def _discover_with(
        self, client: httpx.AsyncClient, endpoint: str, auth: McpAuth
    ) -> list[DiscoveredTool]:
        base_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **auth.headers(),
        }

        init = await self._rpc(
            client,
            endpoint,
            base_headers,
            rpc_id=1,
            method="initialize",
            params={
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        )
        session_id = init.session_id
        self._raise_for_rpc_error(init.payload, "initialize")

        post_init = {**base_headers, "MCP-Protocol-Version": PROTOCOL_VERSION}
        if session_id:
            post_init["Mcp-Session-Id"] = session_id

        # ``notifications/initialized`` is a fire-and-forget notification (no id).
        await self._notify(client, endpoint, post_init, method="notifications/initialized")

        listed = await self._rpc(
            client, endpoint, post_init, rpc_id=2, method="tools/list", params={}
        )
        self._raise_for_rpc_error(listed.payload, "tools/list")
        return self._parse_tools(listed.payload)

    # --- transport helpers ----------------------------------------------------

    async def _rpc(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        headers: dict[str, str],
        *,
        rpc_id: int,
        method: str,
        params: dict[str, Any],
    ) -> _RpcResult:
        body = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
        try:
            resp = await client.post(endpoint, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise McpConnectionError(f"{method}: request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise McpConnectionError(
                f"{method}: server returned HTTP {resp.status_code}."
            )
        raw = resp.content
        if len(raw) > self._max_bytes:
            raise McpConnectionError(f"{method}: response too large.")
        payload = _decode_jsonrpc(raw, resp.headers.get("content-type", ""), rpc_id)
        if payload is None:
            raise McpConnectionError(f"{method}: no JSON-RPC response found.")
        return _RpcResult(payload=payload, session_id=resp.headers.get("mcp-session-id"))

    async def _notify(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        headers: dict[str, str],
        *,
        method: str,
    ) -> None:
        body = {"jsonrpc": "2.0", "method": method}
        try:
            await client.post(endpoint, headers=headers, json=body)
        except httpx.HTTPError:
            # A notification has no response; a failure here shouldn't abort
            # discovery (the subsequent tools/list call will surface real issues).
            return

    @staticmethod
    def _raise_for_rpc_error(payload: dict[str, Any], method: str) -> None:
        error = payload.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise McpConnectionError(f"{method}: server error: {message}.")

    @staticmethod
    def _parse_tools(payload: dict[str, Any]) -> list[DiscoveredTool]:
        result = payload.get("result")
        if not isinstance(result, dict):
            raise McpConnectionError("tools/list: malformed result.")
        raw_tools = result.get("tools")
        if not isinstance(raw_tools, list):
            raise McpConnectionError("tools/list: result has no tools array.")
        tools: list[DiscoveredTool] = []
        for raw in raw_tools[:MAX_TOOLS_PER_SERVER]:
            if not isinstance(raw, dict):
                continue
            name = raw.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            description = raw.get("description")
            schema = raw.get("inputSchema")
            tools.append(
                DiscoveredTool(
                    name=name.strip()[:MAX_TOOL_NAME_LEN],
                    description=(description or "")[:MAX_TOOL_DESCRIPTION_LEN]
                    if isinstance(description, str)
                    else "",
                    inputSchema=schema if isinstance(schema, dict) else {},
                )
            )
        return tools


@dataclass(frozen=True)
class _RpcResult:
    payload: dict[str, Any]
    session_id: str | None


def _decode_jsonrpc(
    raw: bytes, content_type: str, expected_id: int
) -> dict[str, Any] | None:
    """Decode a JSON-RPC response from either a JSON body or an SSE stream."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    if "text/event-stream" in content_type.lower():
        return _decode_sse(text, expected_id)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # Some servers send SSE without the precise content-type; try anyway.
        return _decode_sse(text, expected_id)
    return _match_response(payload, expected_id)


def _decode_sse(text: str, expected_id: int) -> dict[str, Any] | None:
    """Scan SSE events for the JSON-RPC response matching ``expected_id``."""
    # Events are separated by a blank line; an event's data is the join of its
    # ``data:`` field values.
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data_lines = [
            line[len("data:"):].lstrip()
            for line in block.split("\n")
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        matched = _match_response(payload, expected_id)
        if matched is not None:
            return matched
    return None


def _match_response(payload: Any, expected_id: int) -> dict[str, Any] | None:
    """Return ``payload`` if it is the JSON-RPC response for ``expected_id``."""
    if isinstance(payload, list):
        for item in payload:
            matched = _match_response(item, expected_id)
            if matched is not None:
                return matched
        return None
    if not isinstance(payload, dict):
        return None
    if "result" not in payload and "error" not in payload:
        return None
    rid = payload.get("id")
    # Tolerate string/int id mismatch (some servers echo ids as strings).
    if rid == expected_id or str(rid) == str(expected_id):
        return payload
    return None


class FakeMcpConnector:
    """Deterministic connector for tests: returns canned tools or raises."""

    def __init__(
        self,
        tools: list[DiscoveredTool] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._tools = tools or []
        self._error = error
        self.calls: list[tuple[str, McpAuth]] = []

    async def discover(self, *, endpoint: str, auth: McpAuth) -> list[DiscoveredTool]:
        self.calls.append((endpoint, auth))
        if self._error is not None:
            raise self._error
        return list(self._tools)
