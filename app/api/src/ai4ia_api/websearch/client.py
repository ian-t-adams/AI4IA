"""Lazy WebIQ v3 client using the official SDK's public auth and transport APIs.

The SDK's generated resource methods are narrower than the service: 0.1.6
documents domain/custom-search filters that its web method cannot accept, and
0.1.7 removes classic entirely. A fixed, verified REST contract on the SDK's
public AsyncHttpTransport preserves those capabilities without private client
attributes, invented routes, CLI credentials, or a second authentication stack.

Endpoint, credentials, strict safe search, timeout and retries are server-owned.
No automatic retries or crawl polling can multiply a metered tool invocation.
Responses remain plain JSON, including dynamic/nested provider fields; the
capability bounds, redacts and nonce-fences them before returning to the model.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ..config import Settings
from ..http_retry import parse_retry_after
from .contracts import (
    MAX_CONTENT_CHARS,
    MAX_RESULTS,
    STRICT_SEARCH_TOOLS,
    TOOL_PATHS,
    prepare_arguments,
    wire_payload,
)

logger = logging.getLogger(__name__)

ERROR_CONFIG = "config"
ERROR_CREDENTIAL = "credential"
ERROR_AUTH = "auth"
ERROR_PERMISSION = "permission"
ERROR_RATE_LIMIT = "rate_limit"
ERROR_TIMEOUT = "timeout"
ERROR_CONNECTION = "connection"
ERROR_BAD_REQUEST = "bad_request"
ERROR_NOT_FOUND = "not_found"
ERROR_SERVER = "server_error"
ERROR_STATUS = "status"
ERROR_UNKNOWN = "unknown"


class WebSearchError(Exception):
    """A normalized Web IQ failure with a coarse, user-safe category."""

    def __init__(self, category: str, detail: str = "") -> None:
        super().__init__(f"web search error [{category}]: {detail}")
        self.category = category
        self.detail = detail


class WebSearchClient:
    """One lazy, owned SDK transport; injected SDK clients/transports remain caller-owned."""

    def __init__(
        self, settings: Settings, *, sdk_client: Any | None = None, transport: Any | None = None,
    ) -> None:
        self._settings = settings
        self._client = sdk_client
        self._transport = transport
        self._injected_transport = transport is not None
        self._credential: Any | None = None

    def _ensure_transport(self) -> Any:
        if self._transport is not None:
            return self._transport
        from webiq import RetryPolicy
        from webiq.auth import create_auth
        from webiq.transports.http import AsyncHttpTransport

        if self._settings.webiq_api_key:
            auth = create_auth(api_key=self._settings.webiq_api_key)
        elif self._settings.webiq_use_entra:
            from azure.identity.aio import DefaultAzureCredential

            if self._credential is None:
                self._credential = DefaultAzureCredential()
            auth = create_auth(credential=self._credential)
        else:
            raise WebSearchError(ERROR_CONFIG, "No WebIQ credential configured.")
        self._transport = AsyncHttpTransport(
            base_url=self._settings.webiq_base_url or "https://api.microsoft.ai/v3",
            auth=auth,
            retry=RetryPolicy(max_retries=0),
        )
        return self._transport

    def _map_error(self, exc: Exception) -> WebSearchError:
        # Keep raw exception text (which can contain URLs, queries or credentials)
        # out of health diagnostics and logs, not merely out of model results.
        detail = type(exc).__name__
        try:
            from azure.core.exceptions import ClientAuthenticationError

            if isinstance(exc, ClientAuthenticationError):
                return WebSearchError(ERROR_CREDENTIAL, detail)
        except ImportError:
            pass
        try:
            from webiq import (
                APIConnectionError,
                APIStatusError,
                AuthenticationError,
                PermissionDeniedError,
                RateLimitError,
            )
        except ImportError:
            return WebSearchError(ERROR_UNKNOWN, detail)
        if isinstance(exc, AuthenticationError):
            return WebSearchError(ERROR_AUTH, detail)
        if isinstance(exc, PermissionDeniedError):
            return WebSearchError(ERROR_PERMISSION, detail)
        if isinstance(exc, RateLimitError):
            return WebSearchError(ERROR_RATE_LIMIT, detail)
        if isinstance(exc, APIStatusError):
            code = getattr(exc, "status_code", None)
            if isinstance(code, int):
                if code >= 500:
                    return WebSearchError(ERROR_SERVER, detail)
                if code == 404:
                    return WebSearchError(ERROR_NOT_FOUND, detail)
                if code >= 400:
                    return WebSearchError(ERROR_BAD_REQUEST, detail)
            return WebSearchError(ERROR_STATUS, detail)
        if isinstance(exc, APIConnectionError):
            category = ERROR_TIMEOUT if (
                "timed out" in str(exc).lower() or "timeout" in str(exc).lower()
            ) else ERROR_CONNECTION
            return WebSearchError(category, detail)
        return WebSearchError(ERROR_UNKNOWN, detail)

    @staticmethod
    def _plain(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json", exclude_none=True)
        if isinstance(value, Mapping):
            return {key: WebSearchClient._plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [WebSearchClient._plain(item) for item in value]
        if hasattr(value, "__dict__"):
            return WebSearchClient._plain(vars(value))
        return value

    @staticmethod
    def _validate_sdk_payload(name: str, payload: dict[str, Any]) -> None:
        """Exercise the actual generated SDK request contract where one exists."""
        from webiq import types

        models = {
            "web_search": "WebRequest", "news_search": "NewsRequest",
            "video_search": "VideosRequest", "image_search": "ImagesRequest",
            "browse_url": "BrowseRequest", "classic_search": "SearchClassicRequest",
        }
        model = getattr(types, models.get(name, ""), None)
        if model is not None:
            # These fields are explicitly documented in SDK 0.1.6's API reference,
            # but missing from its generated WebRequest/resource method.
            extension_fields = {"customSearchConfigId", "includeDomains", "excludeDomains"}
            core = {key: value for key, value in payload.items() if key not in extension_fields}
            model.model_validate(core)

    async def _call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if not self._settings.web_search_enabled:
            raise WebSearchError(ERROR_CONFIG, "Web search is disabled.")
        try:
            arguments = prepare_arguments(
                name, args,
                results_cap=max(1, min(self._settings.web_search_max_results, MAX_RESULTS)),
                content_cap=max(1, min(self._settings.web_search_max_content_chars, MAX_CONTENT_CHARS)),
            )
            payload = wire_payload(name, arguments)
            self._validate_sdk_payload(name, payload)
        except (ValueError, TypeError, KeyError) as exc:
            raise WebSearchError(ERROR_BAD_REQUEST, "Invalid WebIQ request.") from exc
        try:
            if self._client is not None:
                resource = {
                    "web_search": "web", "news_search": "news", "video_search": "videos",
                    "image_search": "images", "browse_url": "browse", "classic_search": "classic",
                    "finance_search": "finance", "places_search": "places", "sports_search": "sports",
                    "sonic_search": "sonic", "web_autosuggest": "autosuggest",
                }[name]
                argument = arguments.pop("url" if name == "browse_url" else "query")
                if name in STRICT_SEARCH_TOOLS:
                    arguments["safe_search"] = "strict"
                method = "fetch" if name == "browse_url" else "search"
                response = await getattr(getattr(self._client, resource), method)(argument, **arguments)
                data = self._plain(response)
            else:
                data = await self._ensure_transport().request(
                    method="POST", path=TOOL_PATHS[name], json=payload,
                )
        except WebSearchError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize transport and credential failures
            raise self._map_error(exc) from exc
        if data is None:
            return {}
        if isinstance(data, list):
            return {"results": data}
        if not isinstance(data, dict):
            raise WebSearchError(ERROR_SERVER, "Invalid WebIQ response.")
        return data

    async def web_search(self, query: str, *, max_results: int, **options: Any) -> dict[str, Any]:
        return await self._call("web_search", {"query": query, "max_results": max_results, **options})

    async def news_search(self, query: str, *, max_results: int, **options: Any) -> dict[str, Any]:
        return await self._call("news_search", {"query": query, "max_results": max_results, **options})

    async def video_search(self, query: str, *, max_results: int, **options: Any) -> dict[str, Any]:
        return await self._call("video_search", {"query": query, "max_results": max_results, **options})

    async def image_search(self, query: str, *, max_results: int, **options: Any) -> dict[str, Any]:
        return await self._call("image_search", {"query": query, "max_results": max_results, **options})

    async def classic_search(self, query: str, **options: Any) -> dict[str, Any]:
        return await self._call("classic_search", {"query": query, **options})

    async def finance_search(self, query: str, **options: Any) -> dict[str, Any]:
        return await self._call("finance_search", {"query": query, **options})

    async def places_search(self, query: str, **options: Any) -> dict[str, Any]:
        return await self._call("places_search", {"query": query, **options})

    async def sports_search(self, query: str, **options: Any) -> dict[str, Any]:
        return await self._call("sports_search", {"query": query, **options})

    async def sonic_search(self, query: str, **options: Any) -> dict[str, Any]:
        return await self._call("sonic_search", {"query": query, **options})

    async def autosuggest(self, query: str, **options: Any) -> dict[str, Any]:
        return await self._call("web_autosuggest", {"query": query, **options})

    async def browse(self, url: str, **options: Any) -> dict[str, Any]:
        page = await self._call("browse_url", {"url": url, **options})
        page["url"] = page.get("url") or url
        raw_retry = page.get("retryAfter")
        page["retry_after"] = parse_retry_after(raw_retry) if raw_retry is not None else None
        return page

    async def close(self) -> None:
        if self._transport is not None and not self._injected_transport:
            try:
                await self._transport.aclose()
            except Exception:  # noqa: BLE001 - best-effort lifecycle cleanup
                logger.debug("web search transport close failed")
        if self._credential is not None:
            try:
                await self._credential.close()
            except Exception:  # noqa: BLE001
                logger.debug("web search credential close failed")
