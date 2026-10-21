"""Bounded anonymous HTTPS GETs with public DNS pinning and no ambient credentials."""

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

from scripts.http_contract import HttpError, MAX_HTTP_BYTES, obj, public_origin, strict_json


class PublicResolver(AbstractResolver):
    """Connect only to the public addresses returned for the approved origin."""

    def __init__(self, hosts: set[str]) -> None:
        self.hosts = frozenset(hosts)
        self._delegate = aiohttp.resolver.ThreadedResolver()

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET,
    ) -> list[ResolveResult]:
        if host not in self.hosts or port != 443:
            raise HttpError("invalid_configuration")
        async with asyncio.timeout(5):
            results = await self._delegate.resolve(host, port, socket.AddressFamily(family))
        if not results or len(results) > 32:
            raise HttpError("network_unavailable")
        for result in results:
            address = ipaddress.ip_address(result["host"])
            if not address.is_global or address.is_multicast or address.is_unspecified:
                raise HttpError("invalid_configuration")
            if result["hostname"] != host or result["port"] != 443:
                raise HttpError("invalid_configuration")
        return results

    async def close(self) -> None:
        await self._delegate.close()


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    elapsed: float
    content_type: str

    def object(self) -> dict[str, Any]:
        if self.content_type != "application/json":
            raise HttpError("invalid_response")
        return obj(strict_json(self.body, limit=MAX_HTTP_BYTES))


class Transport:
    def __init__(self, origins: set[str]) -> None:
        self.origins = frozenset(public_origin(value) for value in origins)
        self._client: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> Transport:
        self._client = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                resolver=PublicResolver({urlsplit(value).hostname or "" for value in self.origins}),
                use_dns_cache=False, limit=2,
            ),
            timeout=aiohttp.ClientTimeout(total=15, connect=5),
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
            auto_decompress=False,
            max_line_size=8192,
            max_field_size=8192,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client is not None:
            await self._client.close()

    async def request(
        self, method: str, url: str, *, timeout: float = 15, limit: int = MAX_HTTP_BYTES,
    ) -> Response:
        parsed = urlsplit(url)
        if (
            method != "GET" or parsed.scheme != "https"
            or f"https://{parsed.netloc}" not in self.origins
            or parsed.fragment or parsed.username is not None
            or re.search(r"[\x00-\x20\x7f\\]", url)
        ):
            raise HttpError("invalid_configuration")
        if self._client is None:
            raise RuntimeError("Transport is not open.")
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout):
                async with self._client.request(
                    "GET", url, headers={
                        "Accept": "application/json", "Accept-Encoding": "identity",
                        "Cache-Control": "no-store",
                    },
                    allow_redirects=False, timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    if 300 <= response.status < 400:
                        raise HttpError("redirect_rejected")
                    if response.headers.get("Content-Encoding", "identity") != "identity":
                        raise HttpError("invalid_response")
                    if response.content_length is not None and response.content_length > limit:
                        raise HttpError("response_too_large")
                    chunks = bytearray()
                    async for chunk in response.content.iter_chunked(4096):
                        chunks.extend(chunk)
                        if len(chunks) > limit:
                            raise HttpError("response_too_large")
                    return Response(
                        response.status, bytes(chunks), time.monotonic() - started,
                        response.content_type,
                    )
        except TimeoutError as exc:
            raise HttpError("deadline") from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise HttpError("network_unavailable") from exc
