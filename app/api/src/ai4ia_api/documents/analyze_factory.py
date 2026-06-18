"""Selects and constructs the inline-attachment analysis path (default-OFF).

Returns ``None`` when the inline code-interpreter feature is disabled (the
default), so the chat hot path never advertises the ``analyze_attachment`` tool and
nothing is constructed — the zero-regression posture, mirroring
:func:`ai4ia_api.library.compute_factory.build_document_compute`.

When enabled, the service reuses the already-constructed ephemeral-attachment blob
store and the Code Interpreter client (Responses API), both injectable for tests; in
deployments the CI client is built from settings and closed in the app lifespan.
Capabilities are built per turn, bound to the authenticated ``user_id`` +
``session_id`` + the turn nonce.
"""
from __future__ import annotations

import logging

from ..code_interpreter.client import CodeInterpreterClient
from ..config import Settings
from ..entitlements.service import EntitlementService
from ..library.compute_factory import build_code_interpreter_client
from ..usage.service import UsageService
from .analyze_capability import Handler, build_analyze_capability
from .ephemeral_store import EphemeralAttachmentStore

logger = logging.getLogger(__name__)


class InlineAttachmentAnalysisService:
    """Bundles the ephemeral store + Code Interpreter client for inline-attachment
    analysis. Holds no per-user state; capabilities are built per turn bound to the
    authenticated identity + the turn nonce."""

    def __init__(
        self,
        *,
        store: EphemeralAttachmentStore,
        code_interpreter: CodeInterpreterClient,
        entitlements: EntitlementService,
        metering: UsageService,
        settings: Settings,
    ) -> None:
        self._store = store
        self._ci = code_interpreter
        self._entitlements = entitlements
        self._metering = metering
        self._settings = settings

    def build_capability(
        self,
        *,
        user_id: str,
        session_id: str,
        nonce: str,
        attachments: list[dict],
    ) -> tuple[list[dict], dict[str, Handler]]:
        """Build the ``analyze_attachment`` tool for this turn."""
        return build_analyze_capability(
            store=self._store,
            code_interpreter=self._ci,
            entitlements=self._entitlements,
            metering=self._metering,
            settings=self._settings,
            user_id=user_id,
            session_id=session_id,
            nonce=nonce,
            attachments=attachments,
        )

    async def close(self) -> None:
        await self._ci.close()


def build_inline_attachment_analysis(
    settings: Settings,
    *,
    store: EphemeralAttachmentStore,
    entitlements: EntitlementService,
    metering: UsageService,
    code_interpreter: CodeInterpreterClient | None = None,
) -> InlineAttachmentAnalysisService | None:
    """Construct the inline-attachment analysis service.

    Returns ``None`` when the feature is disabled (default), so the chat hot path
    never advertises the tool — the default-OFF, zero-regression posture. The CI
    client is injectable for tests; otherwise it is built lazily from settings
    (no network/credential acquisition at build time).
    """
    if not settings.inline_document_compute_enabled:
        return None
    ci = code_interpreter if code_interpreter is not None else build_code_interpreter_client(settings)
    return InlineAttachmentAnalysisService(
        store=store,
        code_interpreter=ci,
        entitlements=entitlements,
        metering=metering,
        settings=settings,
    )
