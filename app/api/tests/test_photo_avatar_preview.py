"""The one direct egress: host, DNS, redirect, size, type and PNG checks, paired."""
from __future__ import annotations

import logging
import struct
import zlib

import httpx
import pytest

from ai4ia_api.photo_avatars import preview as preview_module
from ai4ia_api.photo_avatars.catalog import load_photo_avatar_catalog
from ai4ia_api.photo_avatars.preview import (
    PreviewBlocked,
    PreviewRejected,
    PreviewUnavailable,
    fetch_preview,
    inspect_png,
)
from ai4ia_api.photo_avatars.provider import PreviewLink

CATALOG = load_photo_avatar_catalog().preview
HOST = CATALOG.host
PUBLIC_IP = "20.60.1.10"
# Built at runtime so the source holds no secret-shaped literal (.gitleaks.toml entry 7).
TOKEN = "-".join(("synthetic", "sas", "signature", "fixture"))
URL = f"https://{HOST}/container/a/b/c/d/e?sv=2025&sp=r&sig={TOKEN}"


def png(width: int = 1024, height: int = 1024, extra: int = 0) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + header
    return (
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + chunk
        + struct.pack(">I", zlib.crc32(chunk)) + b"\x00" * extra
    )


class Transport(httpx.AsyncBaseTransport):
    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class Resolver:
    def __init__(self, *addresses: str) -> None:
        self.addresses = list(addresses) or [PUBLIC_IP]
        self.calls: list[str] = []

    def __call__(self, host: str) -> list[str]:
        self.calls.append(host)
        return self.addresses


def ok(body: bytes | None = None, content_type: str = "application/octet-stream", **headers) -> httpx.Response:
    return httpx.Response(200, content=png() if body is None else body, headers={"content-type": content_type, **headers})


async def _fetch(response, url: str = URL, resolver: Resolver | None = None):
    transport = Transport(response)
    resolver = resolver or Resolver()
    try:
        image = await fetch_preview(PreviewLink(url), CATALOG, resolver=resolver, inner_transport=transport)
    except (PreviewRejected, PreviewBlocked, PreviewUnavailable) as exc:
        return exc, transport, resolver
    return image, transport, resolver


async def test_the_exact_provider_host_is_fetched_once_over_a_pinned_connection():
    image, transport, resolver = await _fetch(ok())
    assert (image.width, image.height) == (1024, 1024)
    assert len(image.sha256) == 64
    assert resolver.calls == [HOST]
    (request,) = transport.requests
    # The socket dials the checked address; TLS and Host stay bound to the name.
    assert request.url.host == PUBLIC_IP
    assert request.extensions["sni_hostname"] == HOST
    assert request.headers["host"] == HOST


@pytest.mark.parametrize("url", [
    f"https://{HOST}.attacker.example/x?sig={TOKEN}",
    f"https://evil.{HOST}/x?sig={TOKEN}",
    f"https://{HOST.replace('use2', 'use3')}/x?sig={TOKEN}",
    f"https://{PUBLIC_IP}/x?sig={TOKEN}",
])
async def test_a_host_outside_the_catalog_is_blocked_before_dns_not_failed(url):
    """Not fetched, like every refusal, but not terminal: the catalog may be stale."""
    result, transport, resolver = await _fetch(ok(), url=url)
    assert isinstance(result, PreviewBlocked) and result.code == "host_not_in_catalog"
    assert not isinstance(result, PreviewRejected)
    assert resolver.calls == [] and transport.requests == []
    # Control: the same link on the catalog host is resolved and fetched once.
    control, control_transport, control_resolver = await _fetch(ok(), url=url.replace(
        httpx.URL(url).host, HOST,
    ))
    assert not isinstance(control, Exception)
    assert control_resolver.calls == [HOST] and len(control_transport.requests) == 1


@pytest.mark.parametrize("url", [
    f"http://{HOST}/x?sig={TOKEN}",
    f"https://user:pass@{HOST}/x?sig={TOKEN}",
    f"https://{HOST}:8443/x?sig={TOKEN}",
    f"https://{HOST}/?sig={TOKEN}",
    "not a url at all",
])
async def test_malformed_links_are_refused_permanently_before_dns(url):
    result, transport, resolver = await _fetch(ok(), url=url)
    assert isinstance(result, PreviewRejected)
    assert resolver.calls == [] and transport.requests == []
    control, control_transport, _ = await _fetch(ok())
    assert not isinstance(control, Exception) and len(control_transport.requests) == 1


class FailingResolver:
    """The shared resolver seam, failing the way real lookups do."""

    def __init__(self, failure: str) -> None:
        self.failure = failure
        self.calls: list[str] = []

    def __call__(self, host: str) -> list[str]:
        import socket
        import time

        self.calls.append(host)
        if self.failure == "eai_again":
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        if self.failure == "oserror":
            raise OSError("resolver unreachable")
        if self.failure == "timeout":
            time.sleep(0.3)
            return [PUBLIC_IP]
        return []  # an empty answer


@pytest.mark.parametrize("failure", ["eai_again", "oserror", "timeout", "empty"])
async def test_a_failed_lookup_is_transient_while_a_private_answer_is_permanent(failure, monkeypatch):
    monkeypatch.setattr(preview_module, "DNS_TIMEOUT_SECONDS", 0.05)
    resolver = FailingResolver(failure)
    result, transport, _ = await _fetch(ok(), resolver=resolver)
    assert isinstance(result, PreviewUnavailable) and result.code == "dns_lookup"
    assert resolver.calls == [HOST] and transport.requests == []
    # Control: the same link whose name resolved to a private address is final.
    rejected, rejected_transport, _ = await _fetch(ok(), resolver=Resolver("10.1.2.3"))
    assert isinstance(rejected, PreviewRejected) and rejected.code == "host_not_public"
    assert rejected_transport.requests == []


async def test_a_private_dns_answer_is_refused_without_connecting():
    result, transport, _ = await _fetch(ok(), resolver=Resolver(PUBLIC_IP, "10.1.2.3"))
    assert isinstance(result, PreviewRejected) and result.code == "host_not_public"
    assert transport.requests == []


async def test_redirects_are_refused_not_followed():
    result, transport, _ = await _fetch(
        httpx.Response(302, headers={"Location": "https://attacker.example/steal"}),
    )
    assert isinstance(result, PreviewRejected) and result.code == "redirect"
    assert len(transport.requests) == 1


@pytest.mark.parametrize("content_type, accepted", [
    ("application/octet-stream", True), ("image/png", True), ("image/png; charset=binary", True),
    ("text/html", False), ("image/svg+xml", False), ("", False),
])
async def test_only_catalog_content_types_are_accepted(content_type, accepted):
    result, _, _ = await _fetch(ok(content_type=content_type))
    assert (not isinstance(result, Exception)) is accepted
    if not accepted:
        assert result.code == "content_type"


async def test_size_is_bounded_by_declared_length_and_by_the_stream():
    declared, _, _ = await _fetch(httpx.Response(
        200, content=png(),
        headers={"content-type": "image/png", "content-length": str(CATALOG.maxBytes + 1)},
    ))
    assert isinstance(declared, PreviewRejected) and declared.code == "too_large"

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield png()
            for _ in range(CATALOG.maxBytes // 65536 + 2):
                yield b"\x00" * 65536

    streamed, _, _ = await _fetch(httpx.Response(200, stream=Stream(), headers={"content-type": "image/png"}))
    assert isinstance(streamed, PreviewRejected) and streamed.code == "too_large"
    within, _, _ = await _fetch(ok(png(extra=1024)))
    assert not isinstance(within, Exception)


@pytest.mark.parametrize("body, code", [
    (b"GIF89a" + b"\x00" * 64, "not_png"),
    (png()[:20], "not_png"),
    (png(width=CATALOG.maxDimension + 1), "bad_dimensions"),
    (png(width=0), "bad_dimensions"),
])
async def test_only_real_pngs_within_the_dimension_bound_are_kept(body, code):
    result, _, _ = await _fetch(ok(body))
    assert isinstance(result, PreviewRejected) and result.code == code
    assert inspect_png(png(64, 64), max_dimension=CATALOG.maxDimension) == (64, 64)


@pytest.mark.parametrize("response", [
    httpx.Response(403), httpx.Response(404), httpx.Response(429), httpx.Response(503),
    httpx.ReadTimeout(f"timed out reading {URL}"), httpx.ConnectError(f"cannot reach {URL}"),
])
async def test_expired_links_and_network_faults_are_transient_and_never_leak_the_link(response, caplog):
    caplog.set_level(logging.DEBUG)
    result, _, _ = await _fetch(response)
    assert isinstance(result, PreviewUnavailable)
    # A transport error is never chained: its message can carry the URL.
    assert result.__cause__ is None
    assert result.__context__ is None or result.__suppress_context__ is True
    rendered = " ".join([repr(result), str(result), *(record.getMessage() for record in caplog.records)])
    assert TOKEN not in rendered and HOST not in str(result)


async def test_no_log_record_carries_the_signed_link_with_a_positive_capture_control(caplog):
    caplog.set_level(logging.DEBUG)
    image, _, _ = await _fetch(ok())
    assert not isinstance(image, Exception)
    fetched = [record.getMessage() for record in caplog.records]
    assert all(TOKEN not in message for message in fetched)
    # Control: the same capture sees an ordinary httpx client log the full URL,
    # which is exactly why the fetch drives the pinned transport directly.
    caplog.clear()
    async with httpx.AsyncClient(transport=Transport(ok())) as client:
        await client.get(URL)
    assert any(TOKEN in record.getMessage() for record in caplog.records)
