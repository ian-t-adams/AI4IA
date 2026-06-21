"""Tests for the SSRF egress guard.

The guard is the single chokepoint deciding whether a user-supplied endpoint may
leave our trust boundary, so it is tested exhaustively against the classic SSRF
targets (cloud metadata, loopback, private/link-local ranges, IPv6 forms) plus
the DNS-rebinding case where one resolved record is internal.
"""
from __future__ import annotations

import pytest

from ai4ia_api.agents.ssrf import (
    MAX_URL_LEN,
    SsrfError,
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
    ],
)
def test_blocks_non_public_ip_literals(url):
    with pytest.raises(SsrfError):
        validate_public_https_url(url, resolver=_only(_PUBLIC))


def test_blocks_hostname_resolving_to_private():
    with pytest.raises(SsrfError):
        validate_public_https_url(
            "https://internal.example/rpc", resolver=_only(["10.1.2.3"])
        )


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
