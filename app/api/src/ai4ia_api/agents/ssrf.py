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
  metadata range), multicast, reserved, and unspecified ranges are rejected.
  Resolving *all* records defeats DNS-rebinding tricks where one record is
  public and another is internal.

The resolver is injectable so tests never touch real DNS, and so a caller can
re-validate at connect time (the recommended posture against rebinding).
"""
from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

# A resolver maps a hostname to a list of IP-address strings. The default uses
# the system resolver; tests inject a deterministic stub.
Resolver = Callable[[str], list[str]]

# Hard cap on a single endpoint URL so a pathological value can't blow up logs
# or storage. MCP endpoints are short in practice.
MAX_URL_LEN = 2048


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
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # includes 169.254.0.0/16 cloud metadata
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_public_https_url(url: str, *, resolver: Resolver | None = None) -> str:
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
