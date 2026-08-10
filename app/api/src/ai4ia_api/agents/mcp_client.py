"""MCP client adapter for tool discovery over Streamable HTTP.

We speak the Model Context Protocol's JSON-RPC handshake directly over
``httpx`` (already a dependency) rather than pulling in a heavier SDK: the
discovery surface we need is small and pinning it here keeps the egress path
fully under our control (no redirects, bounded time + size, explicit headers).

The connector is a narrow seam (:class:`McpConnector`) so the service depends on
the capability, not the transport: tests use :class:`FakeMcpConnector`, and the
live :class:`HttpxMcpConnector`'s framing/auth/error handling is unit-tested
with ``httpx.MockTransport`` (no live server required).

Discovery performs the minimal MCP flow: ``initialize`` → ``notifications/
initialized`` → ``tools/list``. Per-turn execution adds
:meth:`McpConnector.call_tool`, which reuses the same handshake then issues a
``tools/call`` request and parses the returned content blocks into a bounded
string — so a governed :class:`~ai4ia_api.agents.tool_exec.ToolDefinition` can run
a remote tool through the exact same registry/redaction machinery as the built-ins.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .mcp_servers import (
    MAX_TOOL_DESCRIPTION_LEN,
    MAX_TOOLS_PER_SERVER,
    DiscoveredTool,
    McpAuthMode,
    McpConnectionError,
    is_valid_remote_tool_name,
)
from .ssrf import Resolver, SsrfError, async_resolve_pinned_ip

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
        if self.mode is McpAuthMode.apim_subscription and self.secret:
            return {"Ocp-Apim-Subscription-Key": self.secret}
        return {}


@dataclass(frozen=True)
class McpToolResult:
    """The outcome of one ``tools/call`` invocation.

    ``content`` is the server's textual content blocks flattened into a single,
    size-bounded string (binary/non-text blocks are noted by type, never inlined);
    ``is_error`` mirrors the MCP ``result.isError`` flag so the caller can surface a
    remote tool error as a structured tool result rather than a transport failure.
    """

    content: str
    is_error: bool = False


class McpConnector(Protocol):
    async def discover(self, *, endpoint: str, auth: McpAuth) -> list[DiscoveredTool]: ...

    async def call_tool(
        self, *, endpoint: str, auth: McpAuth, tool: str, arguments: dict[str, Any]
    ) -> McpToolResult: ...


class _PinnedHttpsTransport(httpx.AsyncBaseTransport):
    """Pins every outbound request to a single pre-validated public IP.

    The connector resolves the endpoint host **once** (through the injected
    resolver) and validates that every returned address is public, then constructs
    this transport bound to that one pinned IP. For each request it rewrites the URL
    host to the pinned IP — so the OS-level ``connect()`` dials exactly that address
    and httpx/httpcore has no hostname left to re-resolve — while preserving the
    original ``Host`` header (set when the request was built) and the TLS SNI/cert
    hostname (via the ``sni_hostname`` request extension). Because the IP was fixed
    by a single up-front resolve+validate, there is no second, independent
    resolution between validation and connect for a DNS rebind to exploit; cert
    verification still runs against the real hostname, never the IP.
    """

    def __init__(self, pinned_ip: str, *, inner: httpx.AsyncBaseTransport) -> None:
        self._pinned_ip = pinned_ip
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.scheme != "https":
            raise SsrfError("Endpoint URL must use https://.")
        original_host = url.host
        if original_host != self._pinned_ip:
            # Connect to the pinned IP, but keep TLS SNI + certificate verification
            # (and the already-set Host header) bound to the real hostname.
            request.url = url.copy_with(host=self._pinned_ip)
            request.extensions = {**request.extensions, "sni_hostname": original_host}
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class HttpxMcpConnector:
    """Live MCP Streamable-HTTP connector.

    ``client`` may be injected (tests pass one backed by ``httpx.MockTransport``);
    otherwise a short-lived client is created per call with redirects disabled
    (a redirect could otherwise bounce an already-validated host to an internal
    one — SSRF defense in depth on top of the URL guard).

    When the connector creates its own client (the production path), it resolves the
    endpoint host **once** through :func:`~ai4ia_api.agents.ssrf.resolve_pinned_ip`
    (rejecting any non-public address) and routes every request through
    :class:`_PinnedHttpsTransport` bound to that one IP. This is a transport-owned
    SSRF guard with a single resolve->validate->pin shared by ``discover`` and
    ``call_tool``: the socket connects to exactly the validated IP, so a DNS rebind
    cannot slip a private address in between validation and connect, and TLS SNI +
    cert verification stay bound to the real hostname regardless of which loop drives
    invocation.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        resolver: Resolver | None = None,
    ) -> None:
        self._client = client
        self._timeout_s = timeout_s
        self._max_bytes = max_bytes
        self._resolver = resolver

    def _new_client(self, pinned_ip: str) -> httpx.AsyncClient:
        """Build a short-lived client whose socket connects are pinned to ``pinned_ip``.

        Redirects stay disabled and every request is rewritten to the pre-validated
        IP, so the egress target is fixed by the single up-front resolve+validate and
        can never drift to a private address before connect.
        """
        transport = _PinnedHttpsTransport(pinned_ip, inner=httpx.AsyncHTTPTransport())
        return httpx.AsyncClient(
            timeout=self._timeout_s, follow_redirects=False, transport=transport
        )

    async def _pin_for(self, endpoint: str) -> str:
        """Resolve+validate the endpoint host ONCE and return the public IP to pin.

        The single chokepoint for the transport-owned guard: the scheme must be
        https and the host must resolve to only public addresses (see
        :func:`~ai4ia_api.agents.ssrf.resolve_pinned_ip`). The returned IP is what
        the per-call client's socket connects to — there is no later, independent
        resolution for a DNS rebind to exploit.
        """
        url = httpx.URL(endpoint)
        if url.scheme != "https":
            raise SsrfError("Endpoint URL must use https://.")
        host = url.host
        if not host:
            raise SsrfError("Endpoint URL must include a host.")
        return await async_resolve_pinned_ip(
            host, resolver=self._resolver, timeout_s=self._timeout_s
        )

    async def _pin_or_raise(self, endpoint: str, method_label: str) -> str:
        try:
            return await self._pin_for(endpoint)
        except SsrfError as exc:
            raise McpConnectionError(
                f"{method_label}: endpoint is not a permitted egress target: {exc}"
            ) from exc

    async def discover(self, *, endpoint: str, auth: McpAuth) -> list[DiscoveredTool]:
        if self._client is not None:
            return await self._discover_with(self._client, endpoint, auth)
        pinned_ip = await self._pin_or_raise(endpoint, "tools/list")
        async with self._new_client(pinned_ip) as client:
            return await self._discover_with(client, endpoint, auth)

    async def call_tool(
        self, *, endpoint: str, auth: McpAuth, tool: str, arguments: dict[str, Any]
    ) -> McpToolResult:
        if self._client is not None:
            return await self._call_with(self._client, endpoint, auth, tool, arguments)
        pinned_ip = await self._pin_or_raise(endpoint, "tools/call")
        async with self._new_client(pinned_ip) as client:
            return await self._call_with(client, endpoint, auth, tool, arguments)

    async def _open_session(
        self, client: httpx.AsyncClient, endpoint: str, auth: McpAuth
    ) -> dict[str, str]:
        """Run ``initialize`` + ``notifications/initialized`` and return the
        headers (protocol version + any negotiated session id) to use for the
        follow-up request (``tools/list`` or ``tools/call``)."""
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
        self._raise_for_rpc_error(init.payload, "initialize")

        post_init = {**base_headers, "MCP-Protocol-Version": PROTOCOL_VERSION}
        if init.session_id:
            post_init["Mcp-Session-Id"] = init.session_id

        # ``notifications/initialized`` is a fire-and-forget notification (no id).
        await self._notify(client, endpoint, post_init, method="notifications/initialized")
        return post_init

    async def _discover_with(
        self, client: httpx.AsyncClient, endpoint: str, auth: McpAuth
    ) -> list[DiscoveredTool]:
        post_init = await self._open_session(client, endpoint, auth)
        listed = await self._rpc(
            client, endpoint, post_init, rpc_id=2, method="tools/list", params={}
        )
        self._raise_for_rpc_error(listed.payload, "tools/list")
        return self._parse_tools(listed.payload)

    async def _call_with(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        auth: McpAuth,
        tool: str,
        arguments: dict[str, Any],
    ) -> McpToolResult:
        post_init = await self._open_session(client, endpoint, auth)
        called = await self._rpc(
            client,
            endpoint,
            post_init,
            rpc_id=2,
            method="tools/call",
            params={"name": tool, "arguments": arguments or {}},
        )
        self._raise_for_rpc_error(called.payload, "tools/call")
        return self._parse_tool_result(called.payload, self._max_bytes)

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
            async with client.stream(
                "POST", endpoint, headers=headers, json=body
            ) as resp:
                if resp.status_code >= 400:
                    raise McpConnectionError(
                        f"{method}: server returned HTTP {resp.status_code}."
                    )
                declared = resp.headers.get("content-length")
                if declared is not None:
                    try:
                        if int(declared) > self._max_bytes:
                            raise McpConnectionError(f"{method}: response too large.")
                    except ValueError:
                        pass
                parts: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > self._max_bytes:
                        raise McpConnectionError(f"{method}: response too large.")
                    parts.append(chunk)
                raw = b"".join(parts)
                content_type = resp.headers.get("content-type", "")
                session_id = resp.headers.get("mcp-session-id")
        except SsrfError as exc:
            # Defense in depth: the pinned transport refused the target (e.g. a
            # non-https URL slipped through). The primary rebind rejection happens
            # up front in _pin_for, before this client is even built.
            raise McpConnectionError(
                f"{method}: endpoint is not a permitted egress target: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise McpConnectionError(f"{method}: transport error.") from exc
        payload = _decode_jsonrpc(raw, content_type, rpc_id)
        if payload is None:
            raise McpConnectionError(f"{method}: no JSON-RPC response found.")
        return _RpcResult(payload=payload, session_id=session_id)

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
            async with client.stream(
                "POST", endpoint, headers=headers, json=body
            ):
                pass
        except SsrfError as exc:
            # Defense in depth: the pinned transport refused the target. The primary
            # rebind rejection happens up front in _pin_for; a notification must still
            # fail closed if it somehow reaches here.
            raise McpConnectionError(
                f"{method}: endpoint is not a permitted egress target: {exc}"
            ) from exc
        except httpx.HTTPError:
            # A notification has no response; a transport hiccup here shouldn't abort
            # discovery (the subsequent tools/list call will surface real issues).
            return

    @staticmethod
    def _raise_for_rpc_error(payload: dict[str, Any], method: str) -> None:
        error = payload.get("error")
        if error:
            raise McpConnectionError(f"{method}: remote protocol error.")

    @staticmethod
    def _parse_tools(payload: dict[str, Any]) -> list[DiscoveredTool]:
        result = payload.get("result")
        if not isinstance(result, dict):
            raise McpConnectionError("tools/list: malformed result.")
        raw_tools = result.get("tools")
        if not isinstance(raw_tools, list):
            raise McpConnectionError("tools/list: result has no tools array.")
        tools: list[DiscoveredTool] = []
        seen_names: set[str] = set()
        for raw in raw_tools:
            if len(tools) >= MAX_TOOLS_PER_SERVER:
                break
            if not isinstance(raw, dict):
                continue
            name = raw.get("name")
            if (
                not isinstance(name, str)
                or not is_valid_remote_tool_name(name)
                or name in seen_names
            ):
                continue
            seen_names.add(name)
            description = raw.get("description")
            schema = raw.get("inputSchema")
            tools.append(
                DiscoveredTool(
                    name=name,
                    rawName=name,
                    description=(description or "")[:MAX_TOOL_DESCRIPTION_LEN]
                    if isinstance(description, str)
                    else "",
                    inputSchema=schema if isinstance(schema, dict) else {},
                )
            )
        return tools

    @classmethod
    def _parse_tool_result(
        cls, payload: dict[str, Any], max_bytes: int
    ) -> McpToolResult:
        result = payload.get("result")
        if not isinstance(result, dict):
            raise McpConnectionError("tools/call: malformed result.")
        is_error = bool(result.get("isError"))
        # The tool's own result content (success or business-logic error, e.g.
        # "invalid date") is a normal chat-turn tool result, not a log/activity
        # line -- it goes through the same credential/secret redaction as any
        # other tool result (see ``runtime.py``'s ``redact_obj``) and is never
        # blanked here, so the model/user still sees a useful reason a call
        # failed. Only exception *messages* raised by this client (connection,
        # RPC-protocol errors above) are fixed, content-free strings, since
        # those can otherwise chain into log output.
        content = cls._content_to_text(result.get("content"), max_bytes)
        return McpToolResult(content=content, is_error=is_error)

    @staticmethod
    def _content_to_text(content: Any, max_bytes: int) -> str:
        """Flatten MCP ``result.content`` blocks into one bounded string.

        MCP returns a list of typed content blocks; ``{"type":"text","text":...}``
        is the common case. Non-text blocks (image/audio/resource) are noted by
        type rather than inlined so a large/binary payload can never blow up the
        context window or the step trace.
        """
        parts: list[str] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                else:
                    parts.append(f"[{btype or 'unknown'} content]")
        elif isinstance(content, str):
            parts.append(content)
        joined = "\n".join(parts)
        encoded = joined.encode("utf-8")
        if len(encoded) > max_bytes:
            return encoded[:max_bytes].decode("utf-8", "ignore") + "...[truncated]"
        return joined


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
        call_results: dict[str, McpToolResult] | None = None,
        call_error: Exception | None = None,
    ) -> None:
        self._tools = tools or []
        self._error = error
        # Per-tool canned results for ``call_tool`` (keyed by tool name); a missing
        # key yields a benign echo so a test need only set what it asserts on.
        self._call_results = call_results or {}
        self._call_error = call_error
        self.calls: list[tuple[str, McpAuth]] = []
        self.tool_calls: list[tuple[str, str, dict[str, Any], McpAuth]] = []

    async def discover(self, *, endpoint: str, auth: McpAuth) -> list[DiscoveredTool]:
        self.calls.append((endpoint, auth))
        if self._error is not None:
            raise self._error
        return list(self._tools)

    async def call_tool(
        self, *, endpoint: str, auth: McpAuth, tool: str, arguments: dict[str, Any]
    ) -> McpToolResult:
        self.tool_calls.append((endpoint, tool, dict(arguments or {}), auth))
        if self._call_error is not None:
            raise self._call_error
        if tool in self._call_results:
            return self._call_results[tool]
        return McpToolResult(content=f"ok:{tool}", is_error=False)
