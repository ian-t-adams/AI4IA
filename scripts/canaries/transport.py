"""Bounded public-HTTPS transport with pinned DNS, no redirects or ambient auth."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

from .contracts import CanaryError, MAX_HTTP_BYTES, obj, public_origin, strict_json


class PublicResolver(AbstractResolver):
    """Resolve only approved hosts; connect to the checked addresses, not a second lookup."""

    def __init__(self, hosts: set[str]) -> None:
        self.hosts = frozenset(hosts)
        self._delegate = aiohttp.resolver.ThreadedResolver()

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET,
    ) -> list[ResolveResult]:
        if host not in self.hosts or port != 443:
            raise CanaryError("invalid_configuration")
        async with asyncio.timeout(5):
            results = await self._delegate.resolve(host, port, socket.AddressFamily(family))
        if not results or len(results) > 32:
            raise CanaryError("network_unavailable")
        for result in results:
            address = ipaddress.ip_address(result["host"])
            if not address.is_global or address.is_multicast or address.is_unspecified:
                raise CanaryError("invalid_configuration")
            if result["hostname"] != host or result["port"] != 443:
                raise CanaryError("invalid_configuration")
        return results

    async def close(self) -> None:
        await self._delegate.close()


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    elapsed: float
    content_type: str

    def json(self) -> Any:
        if self.content_type != "application/json":
            raise CanaryError("invalid_response")
        return strict_json(self.body, limit=MAX_HTTP_BYTES)

    def object(self) -> dict[str, Any]:
        return obj(self.json())


class Transport:
    def __init__(self, origins: set[str], *, correlation_id: str | None = None) -> None:
        self.origins = frozenset(public_origin(value) for value in origins)
        if correlation_id is not None and not re.fullmatch(r"application-canary-[0-9]{1,19}-1", correlation_id):
            raise CanaryError("invalid_configuration")
        self.correlation_id = correlation_id
        self._client: aiohttp.ClientSession | None = None
        self.handshake_protocol: str | None = None

    async def __aenter__(self) -> Transport:
        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(self._reject_redirect)
        trace.on_request_end.append(self._record_handshake)
        self._client = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                resolver=PublicResolver({urlsplit(value).hostname or "" for value in self.origins}),
                use_dns_cache=False, limit=2,
            ),
            timeout=aiohttp.ClientTimeout(total=15, connect=5),
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
            auto_decompress=False,
            trace_configs=[trace],
            max_line_size=8192,
            max_field_size=8192,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client is not None:
            await self._client.close()

    async def _reject_redirect(self, *_: Any) -> None:
        # ws_connect follows redirects internally; the public trace hook runs
        # before a second request, including redirects to the same approved host.
        raise CanaryError("redirect_rejected")

    async def _record_handshake(
        self, _session: aiohttp.ClientSession, _context: Any,
        params: aiohttp.TraceRequestEndParams,
    ) -> None:
        if params.response.status == 101:
            value = params.response.headers.get("X-AI4IA-Realtime-Protocol")
            self.handshake_protocol = value if value in ("ga", "preview") else None

    def client(self, url: str, *, websocket: bool = False) -> aiohttp.ClientSession:
        parsed = urlsplit(url)
        scheme = "wss" if websocket else "https"
        if (
            parsed.scheme != scheme or f"https://{parsed.netloc}" not in self.origins
            or parsed.fragment or parsed.username is not None
            or re.search(r"[\x00-\x20\x7f\\]", url)
        ):
            raise CanaryError("invalid_configuration")
        if self._client is None:
            raise RuntimeError("Transport is not open.")
        return self._client

    async def request(
        self, method: str, url: str, *, token: str | None = None,
        body: bytes | None = None, content_type: str = "application/json",
        timeout: float = 15, limit: int = MAX_HTTP_BYTES,
        headers: dict[str, str] | None = None,
    ) -> Response:
        client = self.client(url)
        if method not in ("GET", "POST", "DELETE"):
            raise CanaryError("invalid_configuration")
        request_headers = {
            "Accept": "application/json", "Accept-Encoding": "identity",
            "Cache-Control": "no-store", **(headers or {}),
        }
        if self.correlation_id is not None:
            request_headers["x-correlation-id"] = self.correlation_id
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            request_headers["Content-Type"] = content_type
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout):
                async with client.request(
                    method, url, data=body, headers=request_headers,
                    allow_redirects=False, timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    if 300 <= response.status < 400:
                        raise CanaryError("redirect_rejected")
                    if response.headers.get("Content-Encoding", "identity") != "identity":
                        raise CanaryError("invalid_response")
                    if response.content_length is not None and response.content_length > limit:
                        raise CanaryError("response_too_large")
                    chunks = bytearray()
                    async for chunk in response.content.iter_chunked(4096):
                        chunks.extend(chunk)
                        if len(chunks) > limit:
                            raise CanaryError("response_too_large")
                    return Response(
                        response.status, bytes(chunks), time.monotonic() - started,
                        response.content_type,
                    )
        except TimeoutError as exc:
            raise CanaryError("deadline") from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise CanaryError("network_unavailable") from exc
