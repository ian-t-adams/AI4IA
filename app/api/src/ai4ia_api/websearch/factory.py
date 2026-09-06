"""Selects and constructs the Web IQ search path (default-OFF).

Returns ``None`` when the web-search feature is disabled (the default), so the chat
hot path never advertises any web tool and no SDK client is constructed — the
zero-regression posture, mirroring
:func:`ai4ia_api.documents.analyze_factory.build_inline_attachment_analysis`.

When enabled, the bundle holds a :class:`~ai4ia_api.websearch.client.WebSearchClient`
(injectable for tests; otherwise built lazily from settings — no network/credential
acquisition until the first search runs), the entitlement gate, and the usage meter.
Capabilities are built per turn, bound to the authenticated ``user_id`` +
``session_id`` + the turn nonce.
"""
from __future__ import annotations

import logging

from ..config import Settings
from ..entitlements.service import EntitlementService
from ..usage.service import UsageService
from .capability import Handler, build_web_search_capability
from .client import WebSearchClient
from .health import WebSearchHealth

logger = logging.getLogger(__name__)


class WebSearchService:
    """Bundles the Web IQ client + entitlement gate + usage meter. Holds no per-user
    state; capabilities are built per turn bound to the authenticated identity + the
    turn nonce."""

    def __init__(
        self,
        *,
        client: WebSearchClient,
        entitlements: EntitlementService,
        metering: UsageService,
        settings: Settings,
        health: WebSearchHealth | None = None,
    ) -> None:
        self._client = client
        self._entitlements = entitlements
        self._metering = metering
        self._settings = settings
        self._health = health

    def build_capability(
        self, *, user_id: str, session_id: str, nonce: str
    ) -> tuple[list[dict], dict[str, Handler]]:
        """Build all Web IQ retrieval tools for this turn."""
        return build_web_search_capability(
            client=self._client,
            entitlements=self._entitlements,
            metering=self._metering,
            settings=self._settings,
            user_id=user_id,
            session_id=session_id,
            nonce=nonce,
            health=self._health,
        )

    async def close(self) -> None:
        await self._client.close()


def build_web_search_service(
    settings: Settings,
    *,
    entitlements: EntitlementService,
    metering: UsageService,
    client: WebSearchClient | None = None,
    health: WebSearchHealth | None = None,
) -> WebSearchService | None:
    """Construct the Web IQ search service.

    Returns ``None`` when the feature is disabled (default), so the chat hot path
    never advertises any web tool — the default-OFF, zero-regression posture. The
    client is injectable for tests; otherwise it is built lazily from settings (the
    underlying SDK client is constructed only on the first search).

    ``health`` (optional) is the process-local diagnostics recorder shared with the
    admin dashboard; failures/successes are recorded to it for operator visibility.
    """
    if not settings.web_search_enabled:
        return None
    web_client = client if client is not None else WebSearchClient(settings)
    return WebSearchService(
        client=web_client,
        entitlements=entitlements,
        metering=metering,
        settings=settings,
        health=health,
    )
