"""Thin async wrapper over the official ``webiq`` SDK's :class:`WebIQAsyncClient`.

Adds the app-side governance shape the synthetic capability relies on:

* The ``webiq`` SDK is **lazy-imported** (inside :meth:`_ensure_client`), so importing
  this module — and booting the app — never requires the SDK unless the feature is
  enabled and a search actually runs. The underlying client is constructed on first
  use from settings: an ``api_key`` when configured, else an EntraID
  ``DefaultAzureCredential`` when ``webiq_use_entra`` is set.
* Every resource method returns **normalized** ``list[dict]`` / ``dict`` of simple
  scalar fields (never SDK model objects), so the capability layer only ever
  sanitizes plain strings.
* Every ``webiq`` ``WebIQError`` subclass — plus an EntraID token-acquisition
  failure and a missing-credential misconfiguration — is mapped to a local
  :class:`WebSearchError` carrying a coarse, remediation-oriented ``category``
  (``config`` / ``credential`` / ``auth`` / ``permission`` / ``rate_limit`` /
  ``timeout`` / ``connection`` / ``bad_request`` / ``not_found`` / ``server_error``
  / ``status`` / ``unknown``); the capability maps those to clean, user-safe error
  strings and the admin panel counts them. The wrapper itself never raises a raw SDK
  exception into the turn.

  The categories are deliberately finer than the SDK's exception classes so the
  admin diagnostics can point at the *fix*: ``credential`` (the managed identity
  could not get a token at all) reads differently from ``auth`` (a token was
  acquired but Web IQ rejected it, i.e. the identity is not entitled); a 5xx
  ``server_error`` (an upstream Web IQ incident) reads differently from a 4xx
  ``bad_request`` (the request the app sent was wrong); and a ``timeout`` (the
  service is slow/overloaded) reads differently from a ``connection`` failure (no
  network path at all).

For unit tests a fake SDK client may be injected via ``sdk_client`` so the error
mapping + normalization can be exercised without the network or a real key.
"""
from __future__ import annotations

import logging
from typing import Any

from ..config import Settings

logger = logging.getLogger(__name__)

# Coarse, remediation-oriented error categories the capability maps to user-safe
# strings and the admin panel counts. Ordered here loosely by "what an operator
# does about it"; the display order lives in ``health.CATEGORY_ORDER``.
ERROR_CONFIG = "config"  # feature on but no api key and no entra fallback configured
ERROR_CREDENTIAL = "credential"  # entra token could not be acquired (identity/IMDS/scope)
ERROR_AUTH = "auth"  # 401: authenticated principal rejected (e.g. not entitled)
ERROR_PERMISSION = "permission"  # 403: credentials valid, operation not permitted
ERROR_RATE_LIMIT = "rate_limit"  # 429/430: rate / concurrency limit
ERROR_TIMEOUT = "timeout"  # client-side timeout (service reachable but slow/overloaded)
ERROR_CONNECTION = "connection"  # could not reach the service at all
ERROR_BAD_REQUEST = "bad_request"  # other 4xx: the request the app sent was wrong
ERROR_NOT_FOUND = "not_found"  # 404: page/route missing (common + expected for browse)
ERROR_SERVER = "server_error"  # 5xx: upstream Web IQ incident
ERROR_STATUS = "status"  # any other non-2xx status
ERROR_UNKNOWN = "unknown"  # uncategorized


class WebSearchError(Exception):
    """A normalized Web IQ failure with a coarse, user-safe ``category``."""

    def __init__(self, category: str, detail: str = "") -> None:
        super().__init__(f"web search error [{category}]: {detail}")
        self.category = category
        self.detail = detail


class WebSearchClient:
    """Async wrapper that constructs + drives a ``WebIQAsyncClient`` lazily."""

    def __init__(self, settings: Settings, *, sdk_client: Any | None = None) -> None:
        self._settings = settings
        # When injected (tests), use the fake directly and never construct a real
        # client (so no SDK import / network / credential acquisition occurs).
        self._client: Any | None = sdk_client
        self._injected = sdk_client is not None
        self._types: Any | None = None
        self._credential: Any | None = None

    # --- construction / lifecycle -------------------------------------------------

    def _ensure_client(self) -> Any:
        """Return the underlying client, constructing the real one on first use.

        The ``webiq`` SDK is imported here (not at module load) so the app boots
        and the tests run without it unless the feature is on and a search runs.
        Auth: an ``api_key`` when configured, else an EntraID credential when
        ``webiq_use_entra`` is set. A misconfiguration (no key, no entra) surfaces
        as a sanitized per-call error rather than a startup/import failure.
        """
        if self._client is not None:
            return self._client
        from webiq import WebIQAsyncClient  # lazy: only when a search actually runs

        kwargs: dict[str, Any] = {}
        if self._settings.webiq_base_url:
            kwargs["base_url"] = self._settings.webiq_base_url
        if self._settings.webiq_api_key:
            kwargs["api_key"] = self._settings.webiq_api_key
        elif self._settings.webiq_use_entra:
            from azure.identity.aio import DefaultAzureCredential

            self._credential = DefaultAzureCredential()
            kwargs["credential"] = self._credential
        else:
            raise WebSearchError(
                ERROR_CONFIG,
                "web search is not configured (no api key or entra credential).",
            )
        self._client = WebIQAsyncClient(**kwargs)
        return self._client

    def _load_types(self) -> Any:
        """Lazily import + cache the SDK enum classes used to shape requests."""
        if self._types is None:
            from webiq.types import (
                BrowseContentFormat,
                ContentFormat,
                ImageAspectRatio,
                ImageSize,
                SafeSearch,
            )

            self._types = {
                "ContentFormat": ContentFormat,
                "BrowseContentFormat": BrowseContentFormat,
                "ImageAspectRatio": ImageAspectRatio,
                "ImageSize": ImageSize,
                "SafeSearch": SafeSearch,
            }
        return self._types

    @staticmethod
    def _to_enum(enum_cls: Any, value: Any) -> Any | None:
        """Coerce a caller-supplied string to an enum member, else ``None``."""
        if value is None:
            return None
        try:
            return enum_cls(value)
        except (ValueError, KeyError, TypeError):
            for member in enum_cls:
                if str(getattr(member, "value", member)).lower() == str(value).lower():
                    return member
                if member.name.lower() == str(value).lower():
                    return member
        return None

    def _map_error(self, exc: Exception) -> WebSearchError:
        """Map a transport / SDK / credential exception to a categorized error.

        Ordering matters. An EntraID *token-acquisition* failure reaches us straight
        from the SDK's auth provider (``credential.get_token``) as an
        ``azure-core`` ``ClientAuthenticationError`` — never wrapped as a ``webiq``
        error — so it is checked first and surfaced as ``credential`` (distinct from
        a Web IQ ``auth`` 401, where a token *was* acquired but rejected). Then the
        specific ``webiq`` status subclasses (401/403/429) are matched before the
        generic :class:`APIStatusError`, whose ``status_code`` is bucketed into
        ``server_error`` (5xx) / ``not_found`` (404) / ``bad_request`` (other 4xx).
        A client-side ``timeout`` is teased out of ``APIConnectionError`` (the SDK
        folds httpx timeouts into it). All imports are lazy + guarded so mapping can
        never itself raise; anything unrecognized is ``unknown``.
        """
        # 1) Managed-identity token could not be acquired at all (no identity
        #    assigned, IMDS unreachable, wrong scope). azure-identity's
        #    CredentialUnavailableError subclasses ClientAuthenticationError, so the
        #    one check covers both. webiq errors do not derive from this, so testing
        #    it first is safe.
        try:
            from azure.core.exceptions import ClientAuthenticationError

            if isinstance(exc, ClientAuthenticationError):
                return WebSearchError(ERROR_CREDENTIAL, str(exc))
        except Exception:  # noqa: BLE001 - azure-identity may be absent; fall through
            pass

        try:
            from webiq import (
                APIConnectionError,
                APIStatusError,
                AuthenticationError,
                PermissionDeniedError,
                RateLimitError,
            )
        except Exception:  # noqa: BLE001 - never let mapping itself raise
            return WebSearchError(ERROR_UNKNOWN, str(exc))
        # 2) Specific status subclasses (each derives from APIStatusError).
        if isinstance(exc, AuthenticationError):
            return WebSearchError(ERROR_AUTH, str(exc))
        if isinstance(exc, PermissionDeniedError):
            return WebSearchError(ERROR_PERMISSION, str(exc))
        if isinstance(exc, RateLimitError):
            return WebSearchError(ERROR_RATE_LIMIT, str(exc))
        # 3) Generic non-2xx: bucket by HTTP status so an operator can tell an
        #    upstream incident (5xx) from a request the app got wrong (other 4xx)
        #    or a missing page/route (404 — common + expected for browse_url).
        if isinstance(exc, APIStatusError):
            code = getattr(exc, "status_code", None)
            if isinstance(code, int):
                if code >= 500:
                    return WebSearchError(ERROR_SERVER, str(exc))
                if code == 404:
                    return WebSearchError(ERROR_NOT_FOUND, str(exc))
                if code >= 400:
                    return WebSearchError(ERROR_BAD_REQUEST, str(exc))
            return WebSearchError(ERROR_STATUS, str(exc))
        # 4) Connection-level: the SDK folds client-side httpx timeouts into
        #    APIConnectionError ("Request timed out after Ns"). Separate them so
        #    "slow/overloaded" reads differently from "no network path"; fall back
        #    to connection if the SDK's message ever changes.
        if isinstance(exc, APIConnectionError):
            if "timed out" in str(exc).lower() or "timeout" in str(exc).lower():
                return WebSearchError(ERROR_TIMEOUT, str(exc))
            return WebSearchError(ERROR_CONNECTION, str(exc))
        return WebSearchError(ERROR_UNKNOWN, str(exc))

    # --- resource methods (return normalized plain dicts) -------------------------

    async def web_search(
        self,
        query: str,
        *,
        max_results: int,
        language: str | None = None,
        region: str | None = None,
        max_length: int | None = None,
    ) -> list[dict[str, Any]]:
        """Web search -> ``[{title, url, content}, ...]`` (compact ``text`` format)."""
        types = self._load_types()
        client = self._ensure_client()
        try:
            resp = await client.web.search(
                query,
                max_results=max_results,
                language=language,
                region=region,
                content_format=types["ContentFormat"].text,
                max_length=max_length,
            )
        except WebSearchError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize every SDK/transport error
            raise self._map_error(exc) from exc
        items = getattr(resp, "webResults", None) or []
        return [
            {
                "title": getattr(r, "title", None),
                "url": getattr(r, "url", None),
                "content": getattr(r, "content", None),
            }
            for r in items
        ]

    async def news_search(
        self,
        query: str,
        *,
        max_results: int,
        language: str | None = None,
        region: str | None = None,
        max_length: int | None = None,
    ) -> list[dict[str, Any]]:
        """News search -> ``[{title, url, source, content}, ...]``."""
        types = self._load_types()
        client = self._ensure_client()
        try:
            resp = await client.news.search(
                query,
                max_results=max_results,
                language=language,
                region=region,
                content_format=types["ContentFormat"].text,
                max_length=max_length,
            )
        except WebSearchError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc
        items = getattr(resp, "newsResults", None) or []
        return [
            {
                "title": getattr(r, "title", None),
                "url": getattr(r, "url", None),
                "source": getattr(r, "source", None),
                "content": getattr(r, "content", None) or getattr(r, "snippet", None),
            }
            for r in items
        ]

    async def video_search(
        self,
        query: str,
        *,
        max_results: int,
        language: str | None = None,
        region: str | None = None,
        freshness: str | None = None,
    ) -> list[dict[str, Any]]:
        """Video search -> ``[{title, url, source, length, views}, ...]``."""
        self._load_types()
        client = self._ensure_client()
        try:
            resp = await client.videos.search(
                query,
                max_results=max_results,
                language=language,
                region=region,
                freshness=freshness,
            )
        except WebSearchError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc
        items = getattr(resp, "videoResults", None) or []
        return [
            {
                "title": getattr(r, "title", None),
                "url": getattr(r, "url", None),
                "source": getattr(r, "publishedBy", None),
                "length": getattr(r, "length", None),
                "views": getattr(r, "viewCount", None),
            }
            for r in items
        ]

    async def image_search(
        self,
        query: str,
        *,
        max_results: int,
        language: str | None = None,
        region: str | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        safe_search: str | None = None,
    ) -> list[dict[str, Any]]:
        """Image search -> ``[{title, url, source, width, height}, ...]``."""
        types = self._load_types()
        client = self._ensure_client()
        try:
            resp = await client.images.search(
                query,
                max_results=max_results,
                language=language,
                region=region,
                aspect_ratio=self._to_enum(types["ImageAspectRatio"], aspect_ratio),
                image_size=self._to_enum(types["ImageSize"], image_size),
                safe_search=self._to_enum(types["SafeSearch"], safe_search),
            )
        except WebSearchError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc
        items = getattr(resp, "imageResults", None) or []
        return [
            {
                "title": getattr(r, "title", None),
                "url": getattr(r, "url", None),
                "source": getattr(r, "hostPageUrl", None),
                "caption": getattr(r, "caption", None),
                "width": getattr(r, "width", None),
                "height": getattr(r, "height", None),
            }
            for r in items
        ]

    async def browse(
        self,
        url: str,
        *,
        max_length: int | None = None,
        live_crawl: str = "none",
    ) -> dict[str, Any]:
        """Fetch one URL -> ``{url, title, content}`` (markdown content)."""
        types = self._load_types()
        client = self._ensure_client()
        try:
            resp = await client.browse.fetch(
                url,
                max_length=max_length,
                live_crawl=live_crawl,
                content_format=types["BrowseContentFormat"].markdown,
            )
        except WebSearchError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc
        return {
            "url": getattr(resp, "url", None) or url,
            "title": getattr(resp, "title", None),
            "content": getattr(resp, "content", None),
        }

    async def close(self) -> None:
        """Best-effort cleanup of the underlying SDK client + any owned credential."""
        # Never close a caller/test-injected client; only one we constructed.
        if self._client is not None and not self._injected:
            aclose = getattr(self._client, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001 - cleanup must never raise
                    logger.debug("web search client close failed", exc_info=True)
        if self._credential is not None:
            close = getattr(self._credential, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    logger.debug("web search credential close failed", exc_info=True)
