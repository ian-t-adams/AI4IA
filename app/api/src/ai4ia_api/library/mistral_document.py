"""Mistral document analysis normalized to the canonical ingest result."""
from __future__ import annotations

import io

from ..catalog import DeploymentOption, ModelCatalog
from ..content_understanding.models import CUResult
from ..gateway.client import ModelGatewayClient

MAX_MISTRAL_DOCUMENT_BYTES = 30 * 1024 * 1024
MAX_MISTRAL_DOCUMENT_PAGES = 30


def pdf_page_count(data: bytes) -> int:
    """Read a PDF page count without extracting or retaining its contents."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are not supported.")
        return len(reader.pages)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The PDF page count could not be read.") from exc


class MistralDocumentError(RuntimeError):
    def __init__(
        self,
        detail: str,
        *,
        deployment: DeploymentOption,
        pages: int | None,
    ) -> None:
        super().__init__(detail)
        self.provider_completed = True
        self.deployment = deployment
        self.pages = pages


class MistralDocumentClient:
    def __init__(self, gateway: ModelGatewayClient, catalog: ModelCatalog) -> None:
        self._gateway = gateway
        self._catalog = catalog

    async def analyze(
        self,
        model_id: str,
        data: bytes,
        content_type: str,
        *,
        correlation_id: str | None = None,
    ) -> tuple[CUResult, DeploymentOption]:
        deployment = self._catalog.resolve_deployment(model_id)
        if deployment is None:
            raise RuntimeError(f"Mistral document model is unavailable: {model_id}")
        raw = await self._gateway.analyze_document(
            deployment=deployment.deploymentName,
            data=data,
            content_type=content_type,
            correlation_id=correlation_id,
        )
        pages = raw.get("pages")
        if not isinstance(pages, list):
            raise MistralDocumentError(
                "Mistral document response omitted pages.",
                deployment=deployment,
                pages=None,
            )
        markdown = "\n\n".join(
            page["markdown"].strip()
            for page in pages
            if isinstance(page, dict)
            and isinstance(page.get("markdown"), str)
            and page["markdown"].strip()
        )
        if not markdown:
            raise MistralDocumentError(
                "Mistral document response contained no Markdown.",
                deployment=deployment,
                pages=len(pages),
            )
        return (
            CUResult(
                status="Succeeded",
                analyzer_id=model_id,
                markdown=markdown,
                contents=[
                    page
                    for page in pages
                    if isinstance(page, dict)
                ],
                raw=raw,
            ),
            deployment,
        )
