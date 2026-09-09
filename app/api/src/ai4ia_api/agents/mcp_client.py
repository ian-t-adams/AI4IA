"""MCP client adapter for tool discovery over Streamable HTTP.

We speak the Model Context Protocol's JSON-RPC handshake directly over
``httpx`` (already a dependency) rather than pulling in a heavier SDK: the
discovery surface we need is small and pinning it here keeps the egress path
fully under our control (no redirects, bounded time + size, explicit headers).

The connector is a narrow seam (:class:`McpConnector`) so the service depends on
the capability, not the transport: tests use :class:`FakeMcpConnector`, and the
live :class:`HttpxMcpConnector`'s framing/auth/error handling is unit-tested
with ``httpx.MockTransport`` (no live server required).

The default 2025-06-18 flow remains ``initialize`` -> ``notifications/initialized``
-> request. Explicitly configured 2026-07-28 servers receive self-contained
requests, without a session or handshake. Neither errors nor cache hints can
change the configured version, grant capabilities, or replay a tool invocation.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from itertools import count
from typing import Any, Protocol

import httpx

from .mcp_servers import (
    MAX_TOOL_DESCRIPTION_LEN,
    MAX_TOOLS_PER_SERVER,
    MAX_RESOURCE_CONTENT_BYTES,
    MAX_RESOURCE_DESCRIPTION_LEN,
    MAX_RESOURCE_NAME_LEN,
    MAX_RESOURCE_URI_LEN,
    MAX_RESOURCES_PER_SERVER,
    MAX_SECRET_LEN,
    DiscoveredResource,
    DiscoveredTool,
    McpAuthMode,
    McpConnectionError,
    McpProtocolVersion,
    is_valid_remote_tool_name,
)
from .mcp_protocol import (
    CAPABILITIES_META,
    CLIENT_INFO,
    CLIENT_INFO_META,
    PROTOCOL_META,
    McpRequestContext,
    header_parameters,
    request_headers,
    validate_resource_uri,
)
from .ssrf import Resolver, SsrfError, async_resolve_pinned_ip

logger = logging.getLogger(__name__)

# Kept for callers that import the legacy default. Never a global cutover switch.
PROTOCOL_VERSION = McpProtocolVersion.legacy.value
_DEFAULT_TIMEOUT_S = 15.0
_DEFAULT_MAX_BYTES = 2_000_000
MAX_CACHE_TTL_MS = 300_000
MAX_CACHE_ENTRIES = 64
MAX_CACHE_BYTES = 4_000_000
MAX_LIST_PAGES = 8
_CACHE_METHODS = frozenset({"server/discover", "tools/list", "resources/list"})
_LEGACY_CONTEXT = McpRequestContext()
_SSE_BOUNDARY = re.compile(br"\r\n\r\n|\n\n|\r\r")

_CacheKey = tuple[McpRequestContext, str, str, str, str, str]


async def _single_chunk(content: bytes):
    if content:
        yield content


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


@dataclass(frozen=True)
class McpResourceResult:
    """One bounded textual MCP resource returned by ``resources/read``."""

    uri: str
    text: str
    mime_type: str | None = None
    truncated: bool = False


@dataclass(frozen=True)
class McpServerDescription:
    """Advisory discovery data, never server instructions or authorization."""

    supported_versions: tuple[str, ...]
    capabilities: dict[str, Any]


class McpConnector(Protocol):
    def invalidate(self, context: McpRequestContext | None = None) -> None: ...

    async def discover_server(
        self, *, endpoint: str, auth: McpAuth, context: McpRequestContext
    ) -> McpServerDescription: ...

    async def discover(
        self, *, endpoint: str, auth: McpAuth, context: McpRequestContext = _LEGACY_CONTEXT
    ) -> list[DiscoveredTool]: ...

    async def list_resources(
        self, *, endpoint: str, auth: McpAuth, context: McpRequestContext = _LEGACY_CONTEXT
    ) -> list[DiscoveredResource]: ...

    async def read_resource(
        self, *, endpoint: str, auth: McpAuth, uri: str,
        context: McpRequestContext = _LEGACY_CONTEXT,
    ) -> McpResourceResult: ...

    async def call_tool(
        self, *, endpoint: str, auth: McpAuth, tool: str, arguments: dict[str, Any],
        context: McpRequestContext = _LEGACY_CONTEXT,
        input_schema: dict[str, Any] | None = None,
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
        self._cache: OrderedDict[_CacheKey, _CacheEntry] = OrderedDict()
        self._cache_bytes = 0
        self._cache_generation = 0
        self._cache_salt = secrets.token_bytes(32)
        self._request_ids = count(2)

    def invalidate(self, context: McpRequestContext | None = None) -> None:
        """Drop only discovery data, including in-flight fills from before invalidation."""
        self._cache_generation += 1
        for key in list(self._cache):
            if context is None or (
                key[0].owner_id == context.owner_id and key[0].server_id == context.server_id
            ):
                self._cache_bytes -= self._cache.pop(key).size

    def _cache_key(
        self, context: McpRequestContext, endpoint: str, auth: McpAuth,
        method: str, params: dict[str, Any],
    ) -> _CacheKey | None:
        if (
            context.protocol_version is not McpProtocolVersion.stateless
            or method not in _CACHE_METHODS
            or not (context.owner_id and context.server_id and context.configuration_revision)
            or "inputResponses" in params or "requestState" in params
        ):
            return None
        secret = auth.secret if auth.mode is not McpAuthMode.none else None
        credential = hmac.digest(self._cache_salt, (secret or "").encode("utf-8"), "sha256").hex()
        return (
            context, endpoint, auth.mode.value, credential, method,
            json.dumps(params, sort_keys=True, separators=(",", ":"), allow_nan=False),
        )

    def _cached(self, key: _CacheKey | None) -> _RpcResult | None:
        if key is None:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None
        if time.monotonic() >= entry.expires:
            self._cache_bytes -= self._cache.pop(key).size
            return None
        self._cache.move_to_end(key)
        return deepcopy(entry.result)

    def _cache_put(self, key: _CacheKey | None, result: _RpcResult, generation: int) -> None:
        if key is None or generation != self._cache_generation:
            return
        payload = result.payload["result"]
        ttl = payload.get("ttlMs", 0)
        # Even a "public" hint stays owner/auth-scoped. Missing/negative hints
        # are immediately stale; resource bodies and tool results are never cached.
        if type(ttl) is not int or ttl <= 0 or payload.get("cacheScope") not in ("public", "private"):
            return
        size = len(json.dumps(result.payload, ensure_ascii=True).encode("utf-8"))
        if size > MAX_CACHE_BYTES:
            return
        if key in self._cache:
            self._cache_bytes -= self._cache.pop(key).size
        while self._cache and (
            len(self._cache) >= MAX_CACHE_ENTRIES or self._cache_bytes + size > MAX_CACHE_BYTES
        ):
            self._cache_bytes -= self._cache.popitem(last=False)[1].size
        self._cache[key] = _CacheEntry(
            deepcopy(result), result.received_at + min(ttl, MAX_CACHE_TTL_MS) / 1000, size
        )
        self._cache_bytes += size

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

    async def discover_server(
        self, *, endpoint: str, auth: McpAuth, context: McpRequestContext
    ) -> McpServerDescription:
        if context.protocol_version is not McpProtocolVersion.stateless:
            raise McpConnectionError("server/discover requires MCP 2026-07-28.")
        response = await self._request(
            endpoint, auth, context, method="server/discover", params={}
        )
        result = response.payload["result"]
        return McpServerDescription(
            supported_versions=tuple(result["supportedVersions"]),
            capabilities=deepcopy(result["capabilities"]),
        )

    async def discover(
        self, *, endpoint: str, auth: McpAuth, context: McpRequestContext = _LEGACY_CONTEXT
    ) -> list[DiscoveredTool]:
        tools: list[DiscoveredTool] = []
        seen: set[str] = set()
        async with aclosing(self._list_pages(endpoint, auth, context, "tools/list")) as pages:
            async for payload in pages:
                for tool in self._parse_tools(
                    payload, modern=context.protocol_version is McpProtocolVersion.stateless
                ):
                    if tool.name not in seen:
                        tools.append(tool)
                        seen.add(tool.name)
                    if len(tools) >= MAX_TOOLS_PER_SERVER:
                        return tools
        return tools

    async def call_tool(
        self, *, endpoint: str, auth: McpAuth, tool: str, arguments: dict[str, Any],
        context: McpRequestContext = _LEGACY_CONTEXT,
        input_schema: dict[str, Any] | None = None,
    ) -> McpToolResult:
        if not is_valid_remote_tool_name(tool) or not isinstance(arguments, dict):
            raise McpConnectionError("tools/call: invalid name or arguments.")
        result = await self._request(
            endpoint, auth, context, method="tools/call",
            params={"name": tool, "arguments": arguments}, input_schema=input_schema,
        )
        return self._parse_tool_result(result.payload, self._max_bytes)

    async def list_resources(
        self, *, endpoint: str, auth: McpAuth, context: McpRequestContext = _LEGACY_CONTEXT
    ) -> list[DiscoveredResource]:
        resources: list[DiscoveredResource] = []
        seen: set[str] = set()
        async with aclosing(self._list_pages(endpoint, auth, context, "resources/list")) as pages:
            async for payload in pages:
                for resource in self._parse_resources(payload):
                    if resource.uri not in seen:
                        resources.append(resource)
                        seen.add(resource.uri)
                    if len(resources) >= MAX_RESOURCES_PER_SERVER:
                        return resources
        return resources

    async def _list_pages(
        self, endpoint: str, auth: McpAuth, context: McpRequestContext, method: str
    ) -> AsyncGenerator[dict[str, Any], None]:
        params: dict[str, Any] = {}
        seen: set[str] = set()
        scope: str | None = None
        self._validate_connection(auth, context)
        try:
            async with self._client_for(endpoint, method) as client:
                # Legacy cursors belong to the initialized session. Keep both
                # that session and its DNS-pinned client until pagination ends.
                headers = await self._open_session(client, endpoint, auth, context)
                for page in range(MAX_LIST_PAGES):
                    response = await self._rpc(
                        client, endpoint, headers,
                        rpc_id=next(self._request_ids) if context.protocol_version is McpProtocolVersion.stateless else page + 2,
                        method=method, params=params, context=context, auth=auth,
                    )
                    result = response.payload["result"]
                    if context.protocol_version is McpProtocolVersion.stateless:
                        page_scope = result.get("cacheScope", "private")
                        if scope is not None and page_scope != scope:
                            raise McpConnectionError(f"{method}: contradictory page cache scopes.")
                        scope = page_scope
                    yield response.payload
                    cursor = result.get("nextCursor")
                    if cursor is None:
                        return
                    if (
                        not isinstance(cursor, str) or not cursor or len(cursor) > 2048
                        or cursor in seen
                    ):
                        raise McpConnectionError(f"{method}: invalid or repeated pagination cursor.")
                    seen.add(cursor)
                    params = {"cursor": cursor}
            raise McpConnectionError(f"{method}: pagination exceeds local bounds.")
        except McpConnectionError:
            self.invalidate(context)
            raise

    async def read_resource(
        self, *, endpoint: str, auth: McpAuth, uri: str,
        context: McpRequestContext = _LEGACY_CONTEXT,
    ) -> McpResourceResult:
        validate_resource_uri(uri)
        result = await self._request(
            endpoint, auth, context, method="resources/read", params={"uri": uri}
        )
        return self._parse_resource_result(result.payload, uri)

    async def _request(
        self, endpoint: str, auth: McpAuth, context: McpRequestContext, *,
        method: str, params: dict[str, Any], input_schema: dict[str, Any] | None = None,
    ) -> _RpcResult:
        self._validate_connection(auth, context)
        params = deepcopy(params)
        input_schema = deepcopy(input_schema)
        async with self._client_for(endpoint, method) as client:
            return await self._request_with(
                client, endpoint, auth, context, method, params, input_schema
            )

    @staticmethod
    def _validate_connection(auth: McpAuth, context: McpRequestContext) -> None:
        if not isinstance(context.protocol_version, McpProtocolVersion):
            raise McpConnectionError("Unsupported configured MCP protocol version.")
        if auth.mode is not McpAuthMode.none and not (auth.secret and auth.secret.strip()):
            raise McpConnectionError("MCP connection credential is unavailable.")
        if auth.mode is not McpAuthMode.none and auth.secret and (
            len(auth.secret) > MAX_SECRET_LEN
            or any(not 0x20 <= ord(char) <= 0x7E for char in auth.secret)
        ):
            raise McpConnectionError("MCP connection credential is not a valid HTTP value.")
    @asynccontextmanager
    async def _client_for(self, endpoint: str, method: str) -> AsyncIterator[httpx.AsyncClient]:
        if self._client is not None:
            yield self._client
        else:
            # Cache hits do not bypass the transport-owned DNS/public-IP check.
            pinned_ip = await self._pin_or_raise(endpoint, method)
            async with self._new_client(pinned_ip) as client:
                yield client

    async def _request_with(
        self, client: httpx.AsyncClient, endpoint: str, auth: McpAuth,
        context: McpRequestContext, method: str, params: dict[str, Any],
        input_schema: dict[str, Any] | None,
    ) -> _RpcResult:
        if method == "tools/call":
            return await self._call_with(
                client, endpoint, auth, params["name"], params["arguments"],
                context=context, input_schema=input_schema,
            )
        if method == "resources/read":
            return await self._read_resource_with(
                client, endpoint, auth, params["uri"], context=context,
            )
        return await self._perform_request(client, endpoint, auth, context, method, params)

    async def _call_with(
        self, client: httpx.AsyncClient, endpoint: str, auth: McpAuth,
        tool: str, arguments: dict[str, Any], *,
        context: McpRequestContext = _LEGACY_CONTEXT,
        input_schema: dict[str, Any] | None = None,
    ) -> _RpcResult:
        """Execution seam includes every handshake/RPC, independent of protocol."""
        return await self._perform_request(
            client, endpoint, auth, context, "tools/call",
            {"name": tool, "arguments": arguments}, input_schema,
        )

    async def _read_resource_with(
        self, client: httpx.AsyncClient, endpoint: str, auth: McpAuth, uri: str, *,
        context: McpRequestContext = _LEGACY_CONTEXT,
    ) -> _RpcResult:
        return await self._perform_request(
            client, endpoint, auth, context, "resources/read", {"uri": uri},
        )

    async def _perform_request(
        self, client: httpx.AsyncClient, endpoint: str, auth: McpAuth,
        context: McpRequestContext, method: str, params: dict[str, Any],
        input_schema: dict[str, Any] | None = None,
    ) -> _RpcResult:
        headers = await self._open_session(client, endpoint, auth, context)
        return await self._rpc(
            client, endpoint, headers,
            rpc_id=next(self._request_ids) if context.protocol_version is McpProtocolVersion.stateless else 2,
            method=method, params=params,
            context=context, auth=auth, input_schema=input_schema,
        )

    async def _open_session(
        self, client: httpx.AsyncClient, endpoint: str, auth: McpAuth,
        context: McpRequestContext,
    ) -> dict[str, str]:
        """Run ``initialize`` + ``notifications/initialized`` and return the
        headers (protocol version + any negotiated session id) to use for the
        follow-up request (``tools/list`` or ``tools/call``)."""
        base_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Accept-Encoding": "identity",
            **auth.headers(),
        }
        if context.protocol_version is McpProtocolVersion.stateless:
            return base_headers

        init = await self._rpc(
            client,
            endpoint,
            base_headers,
            rpc_id=1,
            method="initialize",
            params={
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
            context=context,
            auth=auth,
        )
        result = init.payload["result"]
        if (
            result.get("protocolVersion") != context.protocol_version.value
            or not isinstance(result.get("capabilities"), dict)
        ):
            raise McpConnectionError("initialize: unsupported or contradictory protocol version.")

        post_init = {**base_headers, "MCP-Protocol-Version": PROTOCOL_VERSION}
        if init.session_id:
            post_init["Mcp-Session-Id"] = init.session_id

        # ``notifications/initialized`` is a fire-and-forget notification (no id).
        await self._notify(client, endpoint, post_init, method="notifications/initialized")
        return post_init

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
        context: McpRequestContext,
        auth: McpAuth,
        input_schema: dict[str, Any] | None = None,
    ) -> _RpcResult:
        params = deepcopy(params)
        modern = context.protocol_version is McpProtocolVersion.stateless
        if modern:
            params["_meta"] = {
                PROTOCOL_META: context.protocol_version.value,
                CLIENT_INFO_META: dict(CLIENT_INFO),
                CAPABILITIES_META: {},
            }
        body = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
        mirrors = request_headers(
            body, protocol=context.protocol_version, input_schema=input_schema, secret=auth.secret
        )
        # Only the legacy handshake owns a session header. All routing mirrors
        # come from this body, not from callers or a previous request.
        headers = {
            name: value for name, value in headers.items()
            if not name.lower().startswith("mcp-") and name.lower() != "last-event-id"
        } | mirrors | (
            {"Mcp-Session-Id": headers["Mcp-Session-Id"]}
            if not modern and "Mcp-Session-Id" in headers else {}
        )
        try:
            content = json.dumps(body, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeEncodeError) as exc:
            raise McpConnectionError(f"{method}: invalid JSON request.") from exc
        if len(content) > _DEFAULT_MAX_BYTES:
            raise McpConnectionError(f"{method}: request too large.")
        key = self._cache_key(context, endpoint, auth, method, params)
        cached = self._cached(key)
        if cached is not None:
            cached.payload["id"] = rpc_id
            return cached
        generation = self._cache_generation
        try:
            async with asyncio.timeout(self._timeout_s):
                async with client.stream(
                    "POST", endpoint, headers=headers, content=content, follow_redirects=False
                ) as resp:
                    if resp.status_code != 400 and not 200 <= resp.status_code < 300:
                        raise McpConnectionError(
                            f"{method}: server returned HTTP {resp.status_code}."
                        )
                    self._validate_response_headers(
                        resp.headers, context, method, headers.get("Mcp-Session-Id")
                    )
                    payload = await self._read_response(resp, rpc_id, method, context)
                    if payload is None:
                        raise McpConnectionError(f"{method}: no JSON-RPC response found.")
                    self._raise_for_rpc_error(payload, method, context.protocol_version)
                    if resp.status_code == 400:
                        raise McpConnectionError(f"{method}: server returned HTTP 400.")
                    self._validate_result(payload, method, context.protocol_version)
                    result = _RpcResult(
                        payload=payload,
                        session_id=resp.headers.get("mcp-session-id"),
                        received_at=time.monotonic(),
                    )
        except (asyncio.CancelledError, TimeoutError, httpx.TimeoutException) as exc:
            self.invalidate(context)
            if not modern and method != "initialize":
                # Legacy disconnect is NOT cancellation. Signal the same request,
                # with its auth/session, after the response stream has closed.
                try:
                    async with asyncio.timeout(min(1.0, self._timeout_s)):
                        await self._notify(
                            client, endpoint, headers, method="notifications/cancelled",
                            params={"requestId": rpc_id},
                        )
                except (McpConnectionError, TimeoutError):
                    logger.warning("MCP cancellation notification could not be delivered.")
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise McpConnectionError(f"{method}: request timed out.") from exc
        except SsrfError as exc:
            self.invalidate(context)
            raise McpConnectionError(
                f"{method}: endpoint is not a permitted egress target: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            self.invalidate(context)
            raise McpConnectionError(f"{method}: transport error.") from exc
        except McpConnectionError:
            self.invalidate(context)
            raise
        self._cache_put(key, result, generation)
        return result

    @staticmethod
    def _validate_response_headers(
        headers: httpx.Headers, context: McpRequestContext, method: str, session: str | None
    ) -> None:
        version = headers.get("mcp-protocol-version")
        if version is not None and version != context.protocol_version.value:
            raise McpConnectionError(f"{method}: contradictory response protocol version.")
        assigned = headers.get("mcp-session-id")
        if assigned is None:
            return
        if context.protocol_version is McpProtocolVersion.stateless:
            raise McpConnectionError(f"{method}: stateless response must not assign a session.")
        if (
            not assigned or len(assigned) > 1024
            or any(not 0x21 <= ord(char) <= 0x7E for char in assigned)
            or (method != "initialize" and assigned != session)
        ):
            raise McpConnectionError(f"{method}: invalid or contradictory MCP session.")

    async def _read_response(
        self, resp: httpx.Response, rpc_id: int, method: str, context: McpRequestContext
    ) -> dict[str, Any] | None:
        encoding = (resp.headers.get("content-encoding") or "").strip().lower()
        if encoding not in ("", "identity"):
            raise McpConnectionError(f"{method}: compressed responses are not accepted.")
        declared = resp.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self._max_bytes:
                    raise McpConnectionError(f"{method}: response too large.")
            except ValueError:
                pass
        modern = context.protocol_version is McpProtocolVersion.stateless
        content_type = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if modern and content_type not in ("application/json", "text/event-stream"):
            raise McpConnectionError(f"{method}: unsupported response content type.")
        total = 0
        pending = bytearray()
        chunks = _single_chunk(resp.content) if resp.is_stream_consumed else resp.aiter_raw()

        def notification(name: str) -> None:
            if name in ("notifications/tools/list_changed", "notifications/resources/list_changed"):
                self.invalidate(context)

        async for chunk in chunks:
            total += len(chunk)
            if total > self._max_bytes:
                raise McpConnectionError(f"{method}: response too large.")
            pending.extend(chunk)
            if content_type == "text/event-stream":
                while (boundary := _SSE_BOUNDARY.search(pending)) is not None:
                    block = bytes(pending[:boundary.start()])
                    del pending[:boundary.end()]
                    response = _decode_jsonrpc(
                        block, content_type, rpc_id, strict=modern, notification=notification
                    )
                    if response is not None:
                        return response
        return _decode_jsonrpc(
            bytes(pending), content_type, rpc_id, strict=modern, notification=notification
        )

    async def _notify(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        headers: dict[str, str],
        *,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        try:
            async with asyncio.timeout(self._timeout_s):
                async with client.stream(
                    "POST", endpoint, headers=headers, json=body, follow_redirects=False
                ) as resp:
                    if resp.status_code != 202:
                        raise McpConnectionError(
                            f"{method}: server returned HTTP {resp.status_code}."
                        )
                    self._validate_response_headers(
                        resp.headers, _LEGACY_CONTEXT, method, headers.get("Mcp-Session-Id")
                    )
        except SsrfError as exc:
            # Defense in depth: the pinned transport refused the target. The primary
            # rebind rejection happens up front in _pin_for; a notification must still
            # fail closed if it somehow reaches here.
            raise McpConnectionError(
                f"{method}: endpoint is not a permitted egress target: {exc}"
            ) from exc
        except (httpx.HTTPError, TimeoutError) as exc:
            raise McpConnectionError(f"{method}: transport error.") from exc

    @staticmethod
    def _raise_for_rpc_error(
        payload: dict[str, Any], method: str, protocol: McpProtocolVersion
    ) -> None:
        error = payload.get("error")
        if error is None:
            return
        if (
            not isinstance(error, dict) or type(error.get("code")) is not int
            or not isinstance(error.get("message"), str)
        ):
            raise McpConnectionError(f"{method}: malformed protocol error.")
        if protocol is McpProtocolVersion.stateless:
            code = error["code"]
            if code == -32022:
                data = error.get("data")
                if (
                    not isinstance(data, dict) or data.get("requested") != protocol.value
                    or not isinstance(data.get("supported"), list)
                    or not data["supported"]
                    or not all(isinstance(value, str) for value in data["supported"])
                    or protocol.value in data["supported"]
                ):
                    raise McpConnectionError(f"{method}: contradictory version-negotiation error.")
                raise McpConnectionError(
                    f"{method}: configured MCP protocol is unsupported; select a version explicitly."
                )
            if code == -32020:
                raise McpConnectionError(f"{method}: server rejected MCP header/body metadata.")
            if code == -32021:
                raise McpConnectionError(f"{method}: server requires unsupported client capabilities.")
        raise McpConnectionError(f"{method}: remote protocol error.")

    @staticmethod
    def _validate_result(
        payload: dict[str, Any], method: str, protocol: McpProtocolVersion
    ) -> None:
        result = payload.get("result")
        if not isinstance(result, dict):
            raise McpConnectionError(f"{method}: malformed result.")
        modern = protocol is McpProtocolVersion.stateless
        result_type = result.get("resultType", None if modern else "complete")
        if result_type != "complete":
            raise McpConnectionError(f"{method}: unsupported or missing resultType.")
        meta = result.get("_meta", {})
        if (
            not isinstance(meta, dict)
            or meta.get(PROTOCOL_META, protocol.value) != protocol.value
            or result.get("protocolVersion", protocol.value) != protocol.value
        ):
            raise McpConnectionError(f"{method}: contradictory result protocol metadata.")
        if modern and method in _CACHE_METHODS | {"resources/read"}:
            if (
                ("ttlMs" in result and type(result["ttlMs"]) is not int)
                or ("cacheScope" in result and result["cacheScope"] not in ("public", "private"))
            ):
                raise McpConnectionError(f"{method}: malformed cache hints.")
        if method == "server/discover":
            versions = result.get("supportedVersions")
            capabilities = result.get("capabilities")
            if (
                not isinstance(versions, list) or not 1 <= len(versions) <= 16
                or not all(
                    isinstance(version, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", version)
                    for version in versions
                )
                or protocol.value not in versions
                or not isinstance(capabilities, dict)
                or not all(isinstance(value, dict) for value in capabilities.values())
            ):
                raise McpConnectionError("server/discover: unsupported or contradictory discovery.")
        if method in ("tools/list", "resources/list"):
            field = "tools" if method == "tools/list" else "resources"
            if not isinstance(result.get(field), list):
                raise McpConnectionError(f"{method}: result has no {field} array.")
        if modern and method == "tools/call":
            if (
                ("isError" in result and not isinstance(result["isError"], bool))
                or (not isinstance(result.get("content"), list) and "structuredContent" not in result)
            ):
                raise McpConnectionError("tools/call: malformed result content.")

    @staticmethod
    def _parse_tools(payload: dict[str, Any], *, modern: bool = False) -> list[DiscoveredTool]:
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
            if modern:
                if not isinstance(schema, dict):
                    logger.warning("MCP tool definition rejected: missing input schema.")
                    continue
                try:
                    header_parameters(schema)
                except McpConnectionError:
                    logger.warning("MCP tool definition rejected: unsafe parameter-header schema.")
                    continue
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

    @staticmethod
    def _parse_resources(payload: dict[str, Any]) -> list[DiscoveredResource]:
        result = payload.get("result")
        if not isinstance(result, dict):
            raise McpConnectionError("resources/list: malformed result.")
        raw_resources = result.get("resources")
        if not isinstance(raw_resources, list):
            raise McpConnectionError("resources/list: result has no resources array.")
        resources: list[DiscoveredResource] = []
        seen: set[str] = set()
        for raw in raw_resources:
            if len(resources) >= MAX_RESOURCES_PER_SERVER:
                break
            if not isinstance(raw, dict):
                continue
            uri = raw.get("uri")
            if (
                not isinstance(uri, str)
                or not uri
                or len(uri) > MAX_RESOURCE_URI_LEN
                or uri in seen
                or any(
                    ord(character) < 0x20 or ord(character) == 0x7F
                    for character in uri
                )
            ):
                continue
            seen.add(uri)
            name = raw.get("name")
            description = raw.get("description")
            mime_type = raw.get("mimeType")
            resources.append(
                DiscoveredResource(
                    uri=uri,
                    name=(
                        name[:MAX_RESOURCE_NAME_LEN]
                        if isinstance(name, str)
                        else ""
                    ),
                    description=(
                        description[:MAX_RESOURCE_DESCRIPTION_LEN]
                        if isinstance(description, str)
                        else ""
                    ),
                    mimeType=(
                        mime_type[:128] if isinstance(mime_type, str) else None
                    ),
                )
            )
        return resources

    @staticmethod
    def _parse_resource_result(
        payload: dict[str, Any], requested_uri: str
    ) -> McpResourceResult:
        result = payload.get("result")
        if not isinstance(result, dict):
            raise McpConnectionError("resources/read: malformed result.")
        contents = result.get("contents")
        if not isinstance(contents, list):
            raise McpConnectionError("resources/read: result has no contents array.")
        for item in contents:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri")
            if uri != requested_uri or not isinstance(item.get("text"), str):
                continue
            text = item["text"]
            encoded = text.encode("utf-8")
            truncated = len(encoded) > MAX_RESOURCE_CONTENT_BYTES
            if truncated:
                text = (
                    encoded[:MAX_RESOURCE_CONTENT_BYTES].decode("utf-8", "ignore")
                    + "...[truncated]"
                )
            mime_type = item.get("mimeType")
            return McpResourceResult(
                uri=requested_uri,
                text=text,
                mime_type=(
                    mime_type[:128] if isinstance(mime_type, str) else None
                ),
                truncated=truncated,
            )
        raise McpConnectionError(
            "resources/read: requested textual resource was not returned."
        )

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
        if not content and "structuredContent" in result:
            content = cls._content_to_text(
                json.dumps(result["structuredContent"], ensure_ascii=True, allow_nan=False), max_bytes
            )
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
    received_at: float


@dataclass(frozen=True)
class _CacheEntry:
    result: _RpcResult
    expires: float
    size: int


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON object key.")
        result[name] = value
    return result


def _invalid_constant(_value: str) -> Any:
    raise ValueError("Non-JSON numeric constant.")


def _load_json(text: str, *, strict: bool) -> Any:
    if strict:
        return json.loads(text, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
    return json.loads(text)


def _decode_jsonrpc(
    raw: bytes, content_type: str, expected_id: int, *, strict: bool = False,
    notification: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    """Decode a JSON-RPC response from either a JSON body or an SSE stream."""
    try:
        text = raw.decode("utf-8", errors="strict" if strict else "replace").strip()
    except UnicodeDecodeError as exc:
        raise McpConnectionError("MCP response is not valid UTF-8.") from exc
    if not text:
        return None
    if "text/event-stream" in content_type.lower():
        return _decode_sse(text, expected_id, strict=strict, notification=notification)
    try:
        payload = _load_json(text, strict=strict)
    except (ValueError, RecursionError) as exc:
        if strict:
            raise McpConnectionError("MCP response is not valid JSON.") from exc
        # Some servers send SSE without the precise content-type; try anyway.
        return _decode_sse(text, expected_id)
    return _match_response(payload, expected_id, strict=strict)


def _decode_sse(
    text: str, expected_id: int, *, strict: bool = False,
    notification: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    """Scan SSE events for the JSON-RPC response matching ``expected_id``."""
    # Events are separated by a blank line; an event's data is the join of its
    # ``data:`` field values.
    for block in text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
        data_lines = [
            line[len("data:"):].lstrip()
            for line in block.split("\n")
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        try:
            payload = _load_json("\n".join(data_lines), strict=strict)
        except (ValueError, RecursionError) as exc:
            if strict:
                raise McpConnectionError("MCP stream contains invalid JSON.") from exc
            continue
        if isinstance(payload, dict) and "method" in payload and "id" not in payload:
            name = payload["method"]
            if strict and (
                payload.get("jsonrpc") != "2.0" or not isinstance(name, str)
                or not name.startswith("notifications/")
            ):
                raise McpConnectionError("MCP stream contains an invalid notification.")
            if notification is not None and isinstance(name, str):
                notification(name)
            continue
        matched = _match_response(payload, expected_id, strict=strict)
        if matched is not None:
            return matched
    return None


def _match_response(
    payload: Any, expected_id: int, *, strict: bool = False
) -> dict[str, Any] | None:
    """Return ``payload`` if it is the JSON-RPC response for ``expected_id``."""
    if isinstance(payload, list):
        if strict:
            raise McpConnectionError("MCP response must be a single JSON-RPC message.")
        for item in payload:
            matched = _match_response(item, expected_id)
            if matched is not None:
                return matched
        return None
    if not isinstance(payload, dict):
        return None
    if "method" in payload:
        if strict:
            raise McpConnectionError("MCP server-initiated requests are not supported.")
        return None
    if "result" not in payload and "error" not in payload:
        return None
    if payload.get("jsonrpc") != "2.0" or ("result" in payload and "error" in payload):
        raise McpConnectionError("MCP response has an invalid JSON-RPC envelope.")
    rid = payload.get("id")
    if type(rid) is int and rid == expected_id:
        return payload
    # Preserve the existing legacy adapter's string/int echo compatibility only.
    if not strict and isinstance(rid, str) and rid == str(expected_id):
        return payload
    if strict:
        raise McpConnectionError("MCP response id does not match the request.")
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
        resources: list[DiscoveredResource] | None = None,
        resource_results: dict[str, McpResourceResult] | None = None,
        resource_error: Exception | None = None,
    ) -> None:
        self._tools = tools or []
        self._error = error
        # Per-tool canned results for ``call_tool`` (keyed by tool name); a missing
        # key yields a benign echo so a test need only set what it asserts on.
        self._call_results = call_results or {}
        self._call_error = call_error
        self._resources = resources or []
        self._resource_results = resource_results or {}
        self._resource_error = resource_error
        self.calls: list[tuple[str, McpAuth]] = []
        self.tool_calls: list[tuple[str, str, dict[str, Any], McpAuth]] = []
        self.resource_lists: list[tuple[str, McpAuth]] = []
        self.resource_reads: list[tuple[str, str, McpAuth]] = []
        self.contexts: list[tuple[str, McpRequestContext]] = []
        self.invalidations: list[McpRequestContext | None] = []
        self.server_discoveries: list[tuple[str, McpAuth]] = []
        self.tool_schemas: list[dict[str, Any] | None] = []

    def invalidate(self, context: McpRequestContext | None = None) -> None:
        self.invalidations.append(context)

    async def discover_server(
        self, *, endpoint: str, auth: McpAuth, context: McpRequestContext
    ) -> McpServerDescription:
        self.server_discoveries.append((endpoint, auth))
        self.contexts.append(("server/discover", context))
        if self._error is not None:
            raise self._error
        return McpServerDescription(
            (context.protocol_version.value,), {"tools": {}, "resources": {}}
        )

    async def discover(
        self, *, endpoint: str, auth: McpAuth, context: McpRequestContext = _LEGACY_CONTEXT
    ) -> list[DiscoveredTool]:
        self.calls.append((endpoint, auth))
        self.contexts.append(("tools/list", context))
        if self._error is not None:
            raise self._error
        return list(self._tools)

    async def call_tool(
        self, *, endpoint: str, auth: McpAuth, tool: str, arguments: dict[str, Any],
        context: McpRequestContext = _LEGACY_CONTEXT,
        input_schema: dict[str, Any] | None = None,
    ) -> McpToolResult:
        self.tool_calls.append((endpoint, tool, dict(arguments or {}), auth))
        self.contexts.append(("tools/call", context))
        self.tool_schemas.append(deepcopy(input_schema))
        if self._call_error is not None:
            raise self._call_error
        if tool in self._call_results:
            return self._call_results[tool]
        return McpToolResult(content=f"ok:{tool}", is_error=False)

    async def list_resources(
        self, *, endpoint: str, auth: McpAuth, context: McpRequestContext = _LEGACY_CONTEXT
    ) -> list[DiscoveredResource]:
        self.resource_lists.append((endpoint, auth))
        self.contexts.append(("resources/list", context))
        if self._resource_error is not None:
            raise self._resource_error
        return list(self._resources)

    async def read_resource(
        self, *, endpoint: str, auth: McpAuth, uri: str,
        context: McpRequestContext = _LEGACY_CONTEXT,
    ) -> McpResourceResult:
        self.resource_reads.append((endpoint, uri, auth))
        self.contexts.append(("resources/read", context))
        if self._resource_error is not None:
            raise self._resource_error
        if uri in self._resource_results:
            return self._resource_results[uri]
        raise McpConnectionError("resources/read: resource not found.")
