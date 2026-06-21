"""Selects and constructs the document compute path.

Returns ``None`` when document compute is disabled (or its prerequisites were not
built), so the chat hot path never classifies intent, never advertises the
``run_code`` / ``export_document`` tools, and the version-download endpoint
refuses — the default-OFF, zero-regression posture, layered on top of document
understanding (11A/11B).

When enabled, the bundle reuses the ingestor's backing IO (repository + blob
store) and the already-built retrieval consumer, exactly like
:func:`build_document_retrieval`, so a document the producer indexed is visible to
compute and exports share one source of IO truth. The Code Interpreter client
(Responses API) is injectable for tests; in deployments it is constructed from
settings and closed in the app lifespan.
"""
from __future__ import annotations

import logging

from ..code_interpreter.client import CodeInterpreterClient
from ..config import Settings
from .compute_capability import Handler, build_compute_capability
from .export import DocumentExportService
from .ingest import DocumentIngestor
from .retrieval import DocumentRetrievalService
from .router import IntentRouter, RouteDecision

logger = logging.getLogger(__name__)


class DocumentComputeService:
    """Bundles the intent router, the Code Interpreter client, and the export
    service over a user's *ready* library. Holds no per-user state; capabilities
    are built per turn bound to the authenticated ``user_id`` + the turn nonce."""

    def __init__(
        self,
        *,
        router: IntentRouter,
        retrieval: DocumentRetrievalService,
        export: DocumentExportService,
        code_interpreter: CodeInterpreterClient,
        settings: Settings,
    ) -> None:
        self._router = router
        self._retrieval = retrieval
        self._export = export
        self._ci = code_interpreter
        self._settings = settings

    @property
    def export(self) -> DocumentExportService:
        """The export service (used by the version-download endpoint)."""
        return self._export

    def classify(self, text: str) -> RouteDecision:
        """Deterministically classify a turn (pure; never raises in practice)."""
        return self._router.classify(text)

    def build_capability(
        self, *, user_id: str, nonce: str, email: str | None = None
    ) -> tuple[list[dict], dict[str, Handler]]:
        """Build the ``run_code`` + ``export_document`` tools for this turn."""
        return build_compute_capability(
            retrieval=self._retrieval,
            code_interpreter=self._ci,
            export=self._export,
            settings=self._settings,
            user_id=user_id,
            nonce=nonce,
            email=email,
        )

    async def close(self) -> None:
        await self._ci.close()


def build_code_interpreter_client(settings: Settings) -> CodeInterpreterClient:
    """Construct the Responses API Code Interpreter client from settings.

    Constructed lazily (no network/credential acquisition at build time), so it is
    safe to create whenever compute is enabled; a missing base url surfaces as a
    best-effort per-call error rather than a startup failure (local/dev).
    """
    return CodeInterpreterClient(settings)


def build_document_compute(
    settings: Settings,
    *,
    ingestor: DocumentIngestor | None,
    retrieval: DocumentRetrievalService | None,
    code_interpreter: CodeInterpreterClient | None = None,
) -> DocumentComputeService | None:
    """Construct the compute consumer.

    Returns ``None`` when document compute is disabled, or when its prerequisites
    (the ingestor's IO and the retrieval consumer) were not built — the
    default-OFF, zero-regression posture: no router, no tools, no download path.
    """
    if not settings.document_compute_enabled:
        return None
    if ingestor is None or retrieval is None:
        return None
    export = DocumentExportService(
        library=ingestor.library,
        blob_store=ingestor.blob,
        settings=settings,
    )
    ci = code_interpreter if code_interpreter is not None else build_code_interpreter_client(settings)
    return DocumentComputeService(
        router=IntentRouter(),
        retrieval=retrieval,
        export=export,
        code_interpreter=ci,
        settings=settings,
    )
