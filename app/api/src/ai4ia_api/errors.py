"""Consistent JSON error responses for the AI4IA API.

Every error the API returns shares one body shape::

    {"detail": str, "code": str, "correlation_id": str | None}

``detail`` is the human-readable message (unchanged from what routers raise),
``code`` is a stable machine-readable token derived from the HTTP status, and
``correlation_id`` echoes the per-request id set by the correlation middleware
(:mod:`ai4ia_api.logging_setup`) so a client can quote it when reporting a
failure. Request-validation errors (422) additionally carry an ``errors`` list
with the structured field-level detail FastAPI would otherwise place inline in
``detail`` — keeping ``detail`` a plain string so the body shape never varies.

The handlers preserve ``HTTPException.headers`` (e.g. ``Retry-After`` on a 429,
``WWW-Authenticate`` on a 401) so rate-limit / auth semantics are unchanged.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from .logging_setup import get_correlation_id

# Stable machine-readable token per status code. Anything not listed falls back
# to a coarse client_error / server_error bucket so the field is always set.
_STATUS_CODES: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_413_CONTENT_TOO_LARGE: "payload_too_large",
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: "unsupported_media_type",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "unprocessable_entity",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "internal_error",
    status.HTTP_502_BAD_GATEWAY: "bad_gateway",
    status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
    status.HTTP_504_GATEWAY_TIMEOUT: "gateway_timeout",
}

VALIDATION_ERROR_CODE = "validation_error"


def error_code_for_status(status_code: int) -> str:
    """Map an HTTP status code to a stable machine-readable error code."""
    known = _STATUS_CODES.get(status_code)
    if known is not None:
        return known
    if 400 <= status_code < 500:
        return "client_error"
    if 500 <= status_code < 600:
        return "server_error"
    return "error"


class ErrorResponse(BaseModel):
    """The shared error body returned by every API error response."""

    detail: str
    code: str
    correlation_id: str | None = None


def build_error_payload(
    *, detail: Any, code: str, correlation_id: str | None = None, **extra: Any
) -> dict[str, Any]:
    """Assemble the consistent error body, omitting an absent correlation id."""
    payload: dict[str, Any] = {"detail": detail, "code": code}
    if correlation_id and correlation_id != "-":
        payload["correlation_id"] = correlation_id
    payload.update(extra)
    return payload


def error_response(
    *,
    status_code: int,
    detail: Any,
    code: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    """Build a :class:`JSONResponse` carrying the consistent error body."""
    payload = build_error_payload(
        detail=detail,
        code=code or error_code_for_status(status_code),
        correlation_id=get_correlation_id(),
        **extra,
    )
    return JSONResponse(status_code=status_code, content=payload, headers=headers)


async def http_exception_handler(
    _request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Render any HTTPException with the shared body, preserving its headers."""
    return error_response(
        status_code=exc.status_code,
        detail=exc.detail,
        headers=getattr(exc, "headers", None),
    )


async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Render request-validation failures with the shared body.

    ``detail`` is a stable summary string; the structured field-level errors
    move under ``errors`` so nothing is lost while the shape stays consistent.
    """
    return error_response(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="Request validation failed.",
        code=VALIDATION_ERROR_CODE,
        errors=jsonable_encoder(exc.errors()),
    )


def register_error_handlers(app: FastAPI) -> None:
    """Wire the shared HTTPException + validation handlers onto ``app``."""
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
