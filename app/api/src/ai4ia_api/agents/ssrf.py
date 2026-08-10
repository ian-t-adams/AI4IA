"""SSRF egress guard for user-supplied MCP server endpoints.

A user registering a "bring your own" MCP server hands us a URL we will later
make outbound requests to. Left unguarded, that is a classic **Server-Side
Request Forgery** primitive: a malicious URL could point at the cloud metadata
endpoint (``169.254.169.254``), a loopback admin port, or a private-network
service the API container can reach but the user never should.

This module is the single chokepoint that decides whether a URL is allowed to
leave our trust boundary. It is deliberately *fail-closed* and dependency-free
(``ipaddress`` + ``socket`` only) so it can be unit-tested exhaustively:

* HTTPS only (no ``http``/``file``/``gopher``/…), no embedded credentials.
* The host is resolved to **every** A/AAAA address and each one must be a
  global, public unicast address — private, loopback, link-local (incl. the
  metadata range), carrier-grade NAT, multicast, reserved, and unspecified
  ranges are rejected. Resolving *all* records defeats DNS-rebinding tricks
  where one record is public and another is internal.

The resolver is injectable so tests never touch real DNS, and so a caller can
re-validate at connect time (the recommended posture against rebinding).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import socket
import threading
from collections.abc import Callable
from urllib.parse import urlsplit

from ..logging_setup import emit_security_block
# A resolver maps a hostname to a list of IP-address strings. The default uses
# the system resolver; tests inject a deterministic stub.
Resolver = Callable[[str], list[str]]

# Hard cap on a single endpoint URL so a pathological value can't blow up logs
# or storage. MCP endpoints are short in practice.
MAX_URL_LEN = 2048
DEFAULT_DNS_TIMEOUT_S = 5.0
MAX_CONCURRENT_DNS_RESOLUTIONS = 4
_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_DNS_RESOLUTIONS,
    thread_name_prefix="ai4ia-dns",
)
_DNS_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_DNS_RESOLUTIONS)


class SsrfError(ValueError):
    """A URL was rejected because it is not a safe public HTTPS endpoint."""


def _default_resolver(host: str) -> list[str]:
    # getaddrinfo returns 5-tuples; the address is the first element of sockaddr.
    # AF_UNSPEC yields both A (IPv4) and AAAA (IPv6) records.
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if ``ip`` is anything other than a global, public unicast address."""
    # An IPv4-mapped IPv6 address (``::ffff:127.0.0.1``) must be judged on its
    # embedded v4 address, or a loopback target could slip through as "global".
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return _is_blocked_ip(mapped)
    # ``not is_global`` and the explicit category checks are BOTH required --
    # each one catches a range the other misses, verified against CPython's
    # ``ipaddress`` rather than assumed:
    #
    # * ``not is_global`` alone adds carrier-grade NAT (``100.64.0.0/10``), which
    #   every explicit check below reports False for. That range is routable
    #   inside many hosting/CGNAT networks, so it was a real egress hole.
    # * the explicit checks alone are still needed because ``is_global`` returns
    #   True for the NAT64 well-known prefix (``64:ff9b::/96``), whose embedded
    #   IPv4 destination is not visible to it; ``is_reserved`` catches that.
    #
    # Combining them is strictly more restrictive than either, so this can only
    # ever reject more, never newly allow.
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # includes 169.254.0.0/16 cloud metadata
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_public_https_url(url: str, *, resolver: Resolver | None = None) -> str:
    try:
        return _validate_public_https_url(url, resolver=resolver)
    except SsrfError:
        emit_security_block("ssrf", "endpoint_rejected", "ssrf_guard")
        raise


async def async_validate_public_https_url(
    url: str,
    *,
    resolver: Resolver | None = None,
    timeout_s: float = DEFAULT_DNS_TIMEOUT_S,
) -> str:
    """Validate an endpoint without running user-controlled DNS on the event loop."""
    try:
        return await _run_bounded_dns(
            _validate_public_https_url,
            url,
            resolver=resolver,
            timeout_s=timeout_s,
        )
    except TimeoutError as exc:
        emit_security_block("ssrf", "endpoint_rejected", "ssrf_guard")
        raise SsrfError("Endpoint host resolution timed out.") from exc
    except SsrfError:
        emit_security_block("ssrf", "endpoint_rejected", "ssrf_guard")
        raise


def _validate_public_https_url(url: str, *, resolver: Resolver | None = None) -> str:
    """Validate ``url`` as a safe, public HTTPS endpoint and return its host.

    Raises :class:`SsrfError` if the scheme is not HTTPS, the URL carries
    embedded credentials, the host is missing/unparseable, or the host resolves
    to any non-public address. On success returns the lowercased host (without
    port), suitable for an egress allowlist entry.
    """
    if not url or not isinstance(url, str):
        raise SsrfError("Endpoint URL is required.")
    if len(url) > MAX_URL_LEN:
        raise SsrfError("Endpoint URL is too long.")

    parts = urlsplit(url.strip())
    if parts.scheme.lower() != "https":
        raise SsrfError("Endpoint URL must use https://.")
    if parts.username or parts.password:
        raise SsrfError("Endpoint URL must not contain embedded credentials.")

    host = parts.hostname
    if not host:
        raise SsrfError("Endpoint URL must include a host.")
    host = host.lower()

    try:
        if parts.port is not None and not (0 < parts.port <= 65535):
            raise SsrfError("Endpoint URL has an invalid port.")
    except ValueError as exc:  # urlsplit raises on a malformed port
        raise SsrfError("Endpoint URL has an invalid port.") from exc

    # If the host is a literal IP, judge it directly — never DNS-resolve it.
    literal_ip = _parse_ip_literal(host)
    if literal_ip is not None:
        if _is_blocked_ip(literal_ip):
            raise SsrfError("Endpoint URL resolves to a non-public address.")
        return host

    resolve = resolver or _default_resolver
    try:
        addresses = resolve(host)
    except (OSError, socket.gaierror) as exc:
        raise SsrfError(f"Endpoint host could not be resolved: {host}.") from exc
    if not addresses:
        raise SsrfError(f"Endpoint host did not resolve to any address: {host}.")

    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise SsrfError(f"Endpoint host resolved to an invalid address: {raw}.") from exc
        if _is_blocked_ip(ip):
            # Defeat DNS rebinding: a single internal record fails the whole host.
            raise SsrfError("Endpoint URL resolves to a non-public address.")
    return host


def resolve_pinned_ip(host: str, *, resolver: Resolver | None = None) -> str:
    try:
        return _resolve_pinned_ip(host, resolver=resolver)
    except SsrfError:
        emit_security_block("ssrf", "connection_rejected", "ssrf_guard")
        raise


async def async_resolve_pinned_ip(
    host: str,
    *,
    resolver: Resolver | None = None,
    timeout_s: float = DEFAULT_DNS_TIMEOUT_S,
) -> str:
    """Resolve and validate a connect-time pin in a bounded worker."""
    try:
        return await _run_bounded_dns(
            _resolve_pinned_ip,
            host,
            resolver=resolver,
            timeout_s=timeout_s,
        )
    except TimeoutError as exc:
        emit_security_block("ssrf", "connection_rejected", "ssrf_guard")
        raise SsrfError("Endpoint host resolution timed out.") from exc
    except SsrfError:
        emit_security_block("ssrf", "connection_rejected", "ssrf_guard")
        raise


async def _run_bounded_dns(
    operation,
    value: str,
    *,
    resolver: Resolver | None,
    timeout_s: float,
) -> str:
    """Submit DNS work only while a dedicated worker slot is truly available.

    A timed-out ``getaddrinfo`` thread cannot be interrupted. Its permit is
    therefore released by the concurrent future only when the worker actually
    exits, preventing timed-out lookups from filling an unbounded executor queue.
    """
    if not _DNS_SLOTS.acquire(blocking=False):
        raise SsrfError("Endpoint DNS resolution capacity is exhausted.")
    try:
        future = _DNS_EXECUTOR.submit(operation, value, resolver=resolver)
    except Exception:
        _DNS_SLOTS.release()
        raise
    future.add_done_callback(lambda _future: _DNS_SLOTS.release())
    wrapped = asyncio.wrap_future(future)
    return await asyncio.wait_for(
        asyncio.shield(wrapped), timeout=max(0.001, timeout_s)
    )


def _resolve_pinned_ip(host: str, *, resolver: Resolver | None = None) -> str:
    """Resolve ``host`` to a public IP that an outbound socket can be pinned to.

    This is the *transport-owned* half of the SSRF defense. :func:`validate_public_https_url`
    checks a host string up front, but the address a name resolves to can change
    between that check and the actual ``connect()`` (DNS rebinding). The transport
    calls this immediately before connecting: the host is re-resolved through the
    injected ``resolver``, **every** returned address must be a public unicast
    address, and the first address is returned so the caller can connect to that
    exact IP (while preserving the original ``Host`` header + TLS SNI). A name that
    was public at validate time but flips to a private/loopback/link-local address
    before connect is therefore rejected at the socket layer.

    Raises :class:`SsrfError` if the host is missing, unresolvable, or resolves to
    any non-public address. A literal IP host is judged directly and returned as-is.
    """
    if not host:
        raise SsrfError("Endpoint host is required.")
    host = host.lower()

    literal_ip = _parse_ip_literal(host)
    if literal_ip is not None:
        if _is_blocked_ip(literal_ip):
            raise SsrfError("Endpoint resolves to a non-public address.")
        return host

    resolve = resolver or _default_resolver
    try:
        addresses = resolve(host)
    except (OSError, socket.gaierror) as exc:
        raise SsrfError(f"Endpoint host could not be resolved: {host}.") from exc
    if not addresses:
        raise SsrfError(f"Endpoint host did not resolve to any address: {host}.")

    pinned: str | None = None
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise SsrfError(f"Endpoint host resolved to an invalid address: {raw}.") from exc
        if _is_blocked_ip(ip):
            # Reject the whole host if any record is internal (rebinding defense).
            raise SsrfError("Endpoint resolves to a non-public address.")
        if pinned is None:
            pinned = raw
    assert pinned is not None  # non-empty list with every address public
    return pinned


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the IP if ``host`` is an IPv4/IPv6 literal, else ``None``.

    A bracketed IPv6 literal from a URL (``[::1]``) arrives unbracketed from
    ``urlsplit().hostname``, so a plain parse covers both forms.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None
