"""Finite fixed-route API transport. No SDK, credentials discovery, proxies or redirects."""
from __future__ import annotations

import http.client
import ipaddress
import queue
import re
import socket
import ssl
import time
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import parse_qsl, quote, urlsplit

from .contracts import canonical_bytes, decode_json
from .live_contracts import (
    CLEANUP_REQUEST_RESERVE, CLEANUP_SECONDS_RESERVE, MAX_CASE_SECONDS, MAX_HTTP_REQUESTS,
    MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, MAX_RUN_SECONDS, MAX_TOTAL_RESPONSE_BYTES,
    LiveConfig, LiveError,
)


@dataclass
class Budget:
    clock: Callable[[], float] = time.monotonic
    started: float = field(init=False)
    http_attempts: int = 0
    response_bytes: int = 0

    def __post_init__(self) -> None:
        self.started = self.clock()

    def remaining(self, *, cleanup: bool = False) -> float:
        reserve = 0 if cleanup else CLEANUP_SECONDS_RESERVE
        remaining = self.started + MAX_RUN_SECONDS - reserve - self.clock()
        if remaining <= 0:
            raise LiveError("timeout")
        return remaining

    def begin(self, *, cleanup: bool) -> float:
        reserve = 0 if cleanup else CLEANUP_REQUEST_RESERVE
        if self.http_attempts >= MAX_HTTP_REQUESTS - reserve:
            raise LiveError("bounds")
        timeout = min(MAX_CASE_SECONDS, self.remaining(cleanup=cleanup))
        self.http_attempts += 1
        return timeout

    def require_work(self, requests: int) -> None:
        if requests < 1 or self.http_attempts + requests + CLEANUP_REQUEST_RESERVE > MAX_HTTP_REQUESTS:
            raise LiveError("bounds")
        self.remaining()

    def received(self, size: int, *, cleanup: bool) -> None:
        if size < 0 or self.response_bytes + size > MAX_TOTAL_RESPONSE_BYTES:
            raise LiveError("bounds")
        self.response_bytes += size
        self.remaining(cleanup=cleanup)


@dataclass(frozen=True, repr=False)
class HttpResult:
    status: int
    body: bytes

    def json(self) -> object:
        return decode_json(self.body, MAX_RESPONSE_BYTES, "response_too_large")


class Transport(Protocol):
    def __call__(
        self, method: str, path: str, body: bytes | None, *, timeout: float, cleanup: bool,
    ) -> HttpResult: ...


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float) -> None:
        self.tls_context = ssl.create_default_context()
        super().__init__(host, port=443, timeout=timeout, context=self.tls_context)
        self.address = address

    def connect(self) -> None:
        connection = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self.tls_context.wrap_socket(connection, server_hostname=self.host)
        except BaseException:
            connection.close()
            raise


def public_address(host: str) -> str:
    answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not answers or len(answers) > 16:
        raise LiveError("transport")
    addresses = []
    for answer in answers:
        address = ipaddress.ip_address(answer[4][0])
        if not address.is_global:
            raise LiveError("transport")
        addresses.append(str(address))
    return addresses[0]


class HTTPS:
    def __init__(self, config: LiveConfig, token: str, budget: Budget) -> None:
        host = urlsplit(config.api_origin).hostname
        if host is None:
            raise LiveError("configuration")
        self.host = host
        self.token = token
        self.budget = budget

    def __call__(
        self, method: str, path: str, body: bytes | None, *, timeout: float, cleanup: bool,
    ) -> HttpResult:
        # Socket timeouts alone do not bound DNS or a peer trickling headers.
        # A daemon performs this one attempt; cancellation closes its connection
        # and the bounded worker process owns its remaining lifetime. No retry.
        result: queue.Queue[HttpResult | LiveError] = queue.Queue(maxsize=1)
        cancelled = threading.Event()
        active: list[_PinnedHTTPSConnection] = []
        deadline = self.budget.clock() + min(timeout, self.budget.remaining(cleanup=cleanup))

        def exchange() -> None:
            try:
                value = self._exchange(method, path, body, deadline, cleanup, cancelled, active)
            except LiveError as exc:
                result.put_nowait(exc)
            else:
                result.put_nowait(value)

        threading.Thread(target=exchange, daemon=True).start()
        try:
            value = result.get(timeout=max(0, deadline - self.budget.clock()))
        except queue.Empty:
            cancelled.set()
            for connection in active:
                connection.close()
            raise LiveError("timeout") from None
        if isinstance(value, LiveError):
            raise value
        return value

    def _exchange(
        self, method: str, path: str, body: bytes | None, deadline: float, cleanup: bool,
        cancelled: threading.Event, active: list[_PinnedHTTPSConnection],
    ) -> HttpResult:
        connection = None
        try:
            # Validate every answer, then connect to that exact IP with the
            # original TLS SNI/Host. No second DNS resolution or address retry.
            address = public_address(self.host)
            timeout = min(deadline - self.budget.clock(), self.budget.remaining(cleanup=cleanup))
            if timeout <= 0:
                raise LiveError("timeout")
            connection = _PinnedHTTPSConnection(self.host, address, timeout)
            active.append(connection)
            if cancelled.is_set():
                raise LiveError("timeout")
            connection.request(method, path, body=body, headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json", "Accept-Encoding": "identity",
                "Content-Type": "application/json", "Connection": "close",
            })
            response = connection.getresponse()
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise LiveError("shape")
            size = response.getheader("Content-Length")
            if size is not None and (not size.isdecimal() or int(size) > MAX_RESPONSE_BYTES):
                raise LiveError("bounds")
            data = bytearray()
            while True:
                remaining = min(deadline - self.budget.clock(), self.budget.remaining(cleanup=cleanup))
                if remaining <= 0 or cancelled.is_set():
                    raise LiveError("timeout")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(
                    8192, MAX_RESPONSE_BYTES + 1 - len(data),
                    MAX_TOTAL_RESPONSE_BYTES + 1 - self.budget.response_bytes,
                ))
                if cancelled.is_set():
                    raise LiveError("timeout")
                if not chunk:
                    break
                self.budget.received(len(chunk), cleanup=cleanup)
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise LiveError("bounds")
            return HttpResult(response.status, bytes(data))
        except (TimeoutError, socket.timeout):
            raise LiveError("timeout") from None
        except (OSError, http.client.HTTPException, ValueError):
            raise LiveError("transport") from None
        finally:
            if connection is not None:
                connection.close()


class ApiClient:
    def __init__(self, transport: Transport, budget: Budget, *, accounts_bytes: bool = False) -> None:
        self.transport = transport
        self.budget = budget
        self.owned_sessions: set[str] = set()
        self.accounts_bytes = accounts_bytes

    def created(self, value: object) -> str:
        if (
            not isinstance(value, str)
            or not re.fullmatch(r"[a-f0-9]{32}|[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}", value)
            or value in self.owned_sessions or len(self.owned_sessions) >= 4
        ):
            raise LiveError("shape")
        self.owned_sessions.add(value)
        return value

    def request(
        self, method: str, path: str, body: dict | None = None, *, cleanup: bool = False,
    ) -> HttpResult:
        fixed = {("GET", "/api/models"), ("POST", "/api/sessions"), ("POST", "/api/chat")}
        capability_query = urlsplit(path)
        query = parse_qsl(capability_query.query, keep_blank_values=True)
        capability_read = (
            method == "GET" and capability_query.path == "/api/execution-capabilities"
            and not capability_query.scheme and not capability_query.netloc and not capability_query.fragment
            and len(query) == 2 and query[0] == ("profile", "authored-synthetic-evaluation")
            and query[1][0] == "model"
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", query[1][1])
            and path == (
                "/api/execution-capabilities?profile=authored-synthetic-evaluation&model="
                + quote(query[1][1], safe="")
            )
        )
        if (method, path) not in fixed and not capability_read:
            allowed = {
                (verb, f"/api/sessions/{session}{suffix}")
                for session in self.owned_sessions for verb, suffix in (
                    ("GET", "/messages"), ("DELETE", ""),
                    ("GET", "/deletion"), ("POST", "/deletion/reconcile"),
                )
            }
            if (method, path) not in allowed:
                raise LiveError("configuration")
        if method == "POST" and path == "/api/chat" and body is not None:
            session = body.get("sessionId")
            # Missing BOTH required fields is the read-only schema-capability
            # probe. Even an older server that ignores controls cannot execute it.
            probe = "sessionId" not in body and "content" not in body
            if not probe and session not in self.owned_sessions:
                raise LiveError("configuration")
        payload = canonical_bytes(body) if body is not None else None
        if payload is not None and len(payload) > MAX_REQUEST_BYTES:
            raise LiveError("bounds")
        timeout = self.budget.begin(cleanup=cleanup)
        result = self.transport(method, path, payload, timeout=timeout, cleanup=cleanup)
        if len(result.body) > MAX_RESPONSE_BYTES:
            raise LiveError("bounds")
        if not self.accounts_bytes:
            self.budget.received(len(result.body), cleanup=cleanup)
        if 300 <= result.status < 400:
            raise LiveError("http")
        return result
