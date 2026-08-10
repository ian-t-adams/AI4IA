"""Tests for the SSRF egress guard.

The guard is the single chokepoint deciding whether a user-supplied endpoint may
leave our trust boundary, so it is tested exhaustively against the classic SSRF
targets (cloud metadata, loopback, private/link-local ranges, IPv6 forms) plus
the DNS-rebinding case where one resolved record is internal.
"""
from __future__ import annotations

import time
import threading
import asyncio

import pytest

from ai4ia_api.agents.ssrf import (
    MAX_URL_LEN,
    MAX_CONCURRENT_DNS_RESOLUTIONS,
    SsrfError,
    async_resolve_pinned_ip,
    async_validate_public_https_url,
    resolve_pinned_ip,
    validate_public_https_url,
)

_PUBLIC = ["93.184.216.34"]


def _only(addr: list[str]):
    return lambda _host: list(addr)


def test_allows_public_hostname_returns_lowercased_host():
    host = validate_public_https_url(
        "https://MCP.Example.com/rpc", resolver=_only(_PUBLIC)
    )
    assert host == "mcp.example.com"


def test_allows_public_ip_literal_without_resolving():
    # A public IP literal is judged directly; the resolver must not be consulted.
    def explode(_host):  # pragma: no cover - must never run
        raise AssertionError("resolver should not be called for an IP literal")

    assert validate_public_https_url("https://93.184.216.34/rpc", resolver=explode) == (
        "93.184.216.34"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data",  # cloud metadata
        "https://127.0.0.1/rpc",  # loopback
        "https://10.0.0.5/rpc",  # private class A
        "https://192.168.1.10/rpc",  # private class C
        "https://172.16.5.4/rpc",  # private class B
        "https://169.254.0.1/rpc",  # link-local
        "https://[::1]/rpc",  # IPv6 loopback
        "https://[::ffff:127.0.0.1]/rpc",  # IPv4-mapped loopback
        "https://[fd00::1]/rpc",  # IPv6 unique-local (private)
        # Carrier-grade NAT (RFC 6598). Reported False by every is_private/
        # is_loopback/is_link_local/is_reserved check, so it was reachable until
        # the guard also required `is_global`. Routable inside CGNAT/hosting
        # networks, which makes it a real internal-egress target.
        "https://100.64.0.1/rpc",
        "https://100.127.255.254/rpc",  # top of 100.64.0.0/10
        "https://[64:ff9b::102:304]/rpc",  # NAT64 well-known prefix (is_global True)
        "https://192.0.2.5/rpc",  # TEST-NET-1 documentation range
        "https://198.18.0.1/rpc",  # benchmarking range
        "https://[2001:db8::1]/rpc",  # IPv6 documentation range
        "https://0.0.0.0/rpc",  # unspecified
    ],
)
def test_blocks_non_public_ip_literals(url):
    with pytest.raises(SsrfError):
        validate_public_https_url(url, resolver=_only(_PUBLIC))


@pytest.mark.parametrize("addr", ["100.64.0.1", "100.100.100.100"])
def test_blocks_hostname_resolving_to_carrier_grade_nat(addr):
    """The rebinding path must reject CGNAT too, not just IP literals."""
    with pytest.raises(SsrfError):
        validate_public_https_url("https://mcp.example.com/rpc", resolver=_only([addr]))


def test_pinned_ip_rejects_carrier_grade_nat():
    with pytest.raises(SsrfError):
        resolve_pinned_ip("mcp.example.com", resolver=_only(["100.64.0.1"]))


@pytest.mark.parametrize("addr", ["93.184.216.34", "8.8.8.8", "2606:4700::1111"])
def test_still_allows_genuinely_public_addresses(addr):
    """Guard against over-blocking: tightening must not reject public unicast."""
    assert validate_public_https_url(
        "https://mcp.example.com/rpc", resolver=_only([addr])
    ) == "mcp.example.com"


def test_blocks_hostname_resolving_to_private():
    with pytest.raises(SsrfError):
        validate_public_https_url(
            "https://internal.example/rpc", resolver=_only(["10.1.2.3"])
        )


def test_rejection_emits_bounded_security_event(monkeypatch):
    events: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "ai4ia_api.agents.ssrf.emit_security_block",
        lambda category, reason, source: events.append((category, reason, source)),
    )
    with pytest.raises(SsrfError):
        validate_public_https_url(
            "https://internal.example/rpc", resolver=_only(["10.1.2.3"])
        )
    assert events == [("ssrf", "endpoint_rejected", "ssrf_guard")]


def test_blocks_dns_rebinding_one_bad_record():
    # One public + one internal record must fail the whole host.
    with pytest.raises(SsrfError):
        validate_public_https_url(
            "https://rebind.example/rpc",
            resolver=_only(["93.184.216.34", "127.0.0.1"]),
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/rpc",  # non-https scheme
        "ftp://example.com/rpc",
        "ws://example.com/rpc",
    ],
)
def test_rejects_non_https_scheme(url):
    with pytest.raises(SsrfError):
        validate_public_https_url(url, resolver=_only(_PUBLIC))


def test_rejects_embedded_credentials():
    with pytest.raises(SsrfError):
        validate_public_https_url(
            "https://user:pass@example.com/rpc", resolver=_only(_PUBLIC)
        )


def test_rejects_missing_host():
    with pytest.raises(SsrfError):
        validate_public_https_url("https:///rpc", resolver=_only(_PUBLIC))


def test_rejects_empty_or_oversize_url():
    with pytest.raises(SsrfError):
        validate_public_https_url("", resolver=_only(_PUBLIC))
    with pytest.raises(SsrfError):
        validate_public_https_url(
            "https://example.com/" + "a" * MAX_URL_LEN, resolver=_only(_PUBLIC)
        )


def test_rejects_unresolvable_host():
    def boom(_host):
        raise OSError("nxdomain")

    with pytest.raises(SsrfError):
        validate_public_https_url("https://nope.example/rpc", resolver=boom)


def test_rejects_host_that_resolves_to_nothing():
    with pytest.raises(SsrfError):
        validate_public_https_url("https://empty.example/rpc", resolver=_only([]))


# --- resolve_pinned_ip: transport-owned connect-time guard --------------------


def test_pinned_ip_returns_first_public_address():
    pinned = resolve_pinned_ip(
        "mcp.example.com", resolver=_only(["93.184.216.34", "93.184.216.35"])
    )
    assert pinned == "93.184.216.34"


def test_pinned_ip_returns_literal_ip_without_resolving():
    def explode(_host):  # pragma: no cover - must never run
        raise AssertionError("resolver should not be called for an IP literal")

    assert resolve_pinned_ip("93.184.216.34", resolver=explode) == "93.184.216.34"


def test_pinned_ip_rejects_private_literal():
    with pytest.raises(SsrfError):
        resolve_pinned_ip("127.0.0.1", resolver=_only(_PUBLIC))


def test_pinned_ip_rejects_any_private_record():
    # A rebind that returns one internal record must fail the whole host even if
    # another record is public — the socket could land on the internal one.
    with pytest.raises(SsrfError):
        resolve_pinned_ip(
            "rebind.example", resolver=_only(["93.184.216.34", "10.0.0.1"])
        )


def test_pinned_ip_rejects_unresolvable_and_empty():
    def boom(_host):
        raise OSError("nxdomain")

    with pytest.raises(SsrfError):
        resolve_pinned_ip("nope.example", resolver=boom)
    with pytest.raises(SsrfError):
        resolve_pinned_ip("empty.example", resolver=_only([]))


async def test_async_resolution_times_out_without_blocking_the_event_loop():
    released = threading.Event()

    def slow(_host: str) -> list[str]:
        released.wait(timeout=1)
        return list(_PUBLIC)

    started = time.monotonic()
    try:
        with pytest.raises(SsrfError, match="timed out"):
            await async_validate_public_https_url(
                "https://mcp.example.com/rpc", resolver=slow, timeout_s=0.01
            )
        assert time.monotonic() - started < 0.15
    finally:
        released.set()
        await asyncio.sleep(0.05)


async def test_async_connect_time_resolution_preserves_rebind_rejection():
    with pytest.raises(SsrfError, match="non-public"):
        await async_resolve_pinned_ip(
            "rebind.example",
            resolver=_only(["93.184.216.34", "10.0.0.1"]),
            timeout_s=1,
        )


async def test_async_registration_resolution_allows_public_control():
    assert (
        await async_validate_public_https_url(
            "https://MCP.Example.com/rpc", resolver=_only(_PUBLIC), timeout_s=1
        )
        == "mcp.example.com"
    )


async def test_dns_worker_admission_stays_bounded_after_caller_timeouts():
    release = threading.Event()
    lock = threading.Lock()
    started = 0

    def blocked(_host: str) -> list[str]:
        nonlocal started
        with lock:
            started += 1
        release.wait(timeout=2)
        return list(_PUBLIC)

    timed_out = await asyncio.gather(
        *[
            async_validate_public_https_url(
                f"https://slow-{index}.example/rpc",
                resolver=blocked,
                timeout_s=0.02,
            )
            for index in range(MAX_CONCURRENT_DNS_RESOLUTIONS)
        ],
        return_exceptions=True,
    )
    assert all(
        isinstance(result, SsrfError) and "timed out" in str(result)
        for result in timed_out
    )
    assert started == MAX_CONCURRENT_DNS_RESOLUTIONS

    extra_started = False

    def extra(_host: str) -> list[str]:
        nonlocal extra_started
        extra_started = True
        return list(_PUBLIC)

    with pytest.raises(SsrfError, match="capacity is exhausted"):
        await async_validate_public_https_url(
            "https://overflow.example/rpc", resolver=extra, timeout_s=0.1
        )
    assert extra_started is False

    release.set()
    for _ in range(50):
        try:
            result = await async_validate_public_https_url(
                "https://recovered.example/rpc", resolver=_only(_PUBLIC), timeout_s=1
            )
            break
        except SsrfError as exc:
            if "capacity is exhausted" not in str(exc):
                raise
            await asyncio.sleep(0.01)
    else:  # pragma: no cover - bounded workers must release promptly
        pytest.fail("DNS worker permits did not release")
    assert result == "recovered.example"
