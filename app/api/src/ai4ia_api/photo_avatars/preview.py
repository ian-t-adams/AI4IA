"""The one direct egress: a bounded, one-time copy of the provider preview.

When the provider reports an avatar ``Succeeded``, its status carries
``promptImageUri``: a provider-issued Blob SAS link to the generated portrait,
valid for about 12 hours. AGENTS.md rule 1 approves exactly one exception for
it. FastAPI fetches the link once and copies the image into AI4IA Blob. The link
itself is never stored, logged or returned, and every check below runs before
any byte is kept:

* HTTPS only, no userinfo, the default port, and the catalog's exact provider
  storage host (a lookalike or subdomain is refused before DNS; a host the
  catalog does not name blocks the copy without failing the avatar, so a
  reviewed catalog fix recovers it);
* every DNS answer must be public, and the connection is pinned to the checked
  address so a rebind cannot redirect it (the SSRF helpers the MCP connector
  uses); a lookup that fails or times out is transient, a non-public answer is
  permanent;
* no redirects, a streamed byte cap, an allowed content type, and a real PNG
  whose IHDR dimensions fit the catalog bound.

Errors carry fixed codes only; exceptions from the transport are never chained
into them, because an HTTP client's message can include the URL.
"""
from __future__ import annotations

import hashlib
import logging
import struct
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from ..agents.mcp_client import _PinnedHttpsTransport
from ..agents.ssrf import DnsCapacityError, DnsLookupError, SsrfError, async_resolve_pinned_ip
from ..config import Settings
from ..library.blob_store import AzureBlobStore, BlobNotFoundError, BlobStore, InMemoryBlobStore
from ..logging_setup import emit_security_block
from .catalog import PhotoAvatarPreviewCatalog
from .provider import PreviewLink

logger = logging.getLogger(__name__)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PREVIEW_CONTENT_TYPE = "image/png"
FETCH_TIMEOUT_SECONDS = 20.0
DNS_TIMEOUT_SECONDS = 5.0
AVATARS_DIR = "avatars"

__all__ = [
    "BlobNotFoundError",
    "PreviewBlocked",
    "PreviewImage",
    "PreviewRejected",
    "PreviewUnavailable",
    "PhotoAvatarArtifactStore",
    "build_photo_avatar_blob_store",
    "fetch_preview",
    "inspect_png",
]


class PreviewRejected(Exception):
    """The provider's preview failed a permanent check; it will not be stored."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PreviewBlocked(Exception):
    """A well-formed link on a host the catalog does not name; nothing is fetched.

    Not terminal: the catalog host may be the stale part. Each status read gets
    a fresh link from the provider, so a reviewed catalog change recovers the
    avatar without re-creating it.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PreviewUnavailable(Exception):
    """A transient failure; a later status read gets a fresh link and retries."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PreviewImage:
    data: bytes
    width: int
    height: int
    sha256: str


def inspect_png(data: bytes, *, max_dimension: int) -> tuple[int, int]:
    """The IHDR width and height of a real PNG within the dimension bound."""
    if len(data) < 33 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise PreviewRejected("not_png")
    width, height = struct.unpack(">II", data[16:24])
    if not (1 <= width <= max_dimension and 1 <= height <= max_dimension):
        raise PreviewRejected("bad_dimensions")
    return width, height


def _checked_url(link: PreviewLink, catalog: PhotoAvatarPreviewCatalog) -> httpx.URL:
    try:
        url = httpx.URL(link.reveal())
    except (httpx.InvalidURL, TypeError, ValueError):
        raise PreviewRejected("invalid_link") from None
    if (
        url.scheme != "https"
        or url.userinfo
        or url.port not in (None, 443)
        or not url.path.strip("/")
        or url.fragment
    ):
        emit_security_block("photo_avatar_preview", "host_rejected", "preview_fetch")
        raise PreviewRejected("host_not_allowed")
    if url.host.lower() != catalog.host:
        # Refused before DNS, like every shape check; only the verdict differs.
        emit_security_block("photo_avatar_preview", "host_not_in_catalog", "preview_fetch")
        raise PreviewBlocked("host_not_in_catalog")
    return url


async def fetch_preview(
    link: PreviewLink,
    catalog: PhotoAvatarPreviewCatalog,
    *,
    resolver: Callable[[str], list[str]] | None = None,
    inner_transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = FETCH_TIMEOUT_SECONDS,
) -> PreviewImage:
    """Fetch and validate the preview once. ``inner_transport`` is for tests.

    The pinned transport is driven directly rather than through
    ``httpx.AsyncClient``: the client logs every request URL at INFO, and this
    URL carries the SAS signature. A bare transport also has no redirect,
    cookie, proxy or environment behaviour to disable.
    """
    url = _checked_url(link, catalog)
    try:
        pinned = await async_resolve_pinned_ip(url.host, resolver=resolver, timeout_s=DNS_TIMEOUT_SECONDS)
    except DnsCapacityError:
        raise PreviewUnavailable("dns_capacity") from None
    except DnsLookupError:
        # A timeout or resolver error says nothing about the host; retry later.
        raise PreviewUnavailable("dns_lookup") from None
    except SsrfError:
        # The name resolved, and an answer was not a public address.
        raise PreviewRejected("host_not_public") from None
    transport = _PinnedHttpsTransport(
        pinned, inner=inner_transport or httpx.AsyncHTTPTransport(retries=0, trust_env=False),
    )
    request = httpx.Request(
        "GET", url, extensions={"timeout": httpx.Timeout(timeout).as_dict()},
    )
    try:
        data = await _download(transport, request, catalog)
    finally:
        await transport.aclose()
    width, height = inspect_png(data, max_dimension=catalog.maxDimension)
    return PreviewImage(data, width, height, hashlib.sha256(data).hexdigest())


async def _download(
    transport: httpx.AsyncBaseTransport, request: httpx.Request, catalog: PhotoAvatarPreviewCatalog,
) -> bytes:
    chunks: list[bytes] = []
    received = 0
    try:
        try:
            response = await transport.handle_async_request(request)
        except SsrfError:
            raise PreviewRejected("host_not_public") from None
        try:
            status = response.status_code
            if 300 <= status < 400:
                emit_security_block("photo_avatar_preview", "redirect_refused", "preview_fetch")
                raise PreviewRejected("redirect")
            if status in {403, 404, 408, 429} or status >= 500:
                # An expired or throttled link: the next status read issues a new one.
                raise PreviewUnavailable(f"status_{status}")
            if status != 200:
                raise PreviewRejected(f"status_{status}")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in catalog.contentTypes:
                raise PreviewRejected("content_type")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise PreviewRejected("content_encoding")
            declared = response.headers.get("content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > catalog.maxBytes):
                raise PreviewRejected("too_large")
            stream = response.stream
            if not isinstance(stream, httpx.AsyncByteStream):
                raise PreviewUnavailable("stream")
            async for chunk in stream:
                received += len(chunk)
                if received > catalog.maxBytes:
                    raise PreviewRejected("too_large")
                chunks.append(chunk)
        finally:
            await response.aclose()
    except (PreviewRejected, PreviewUnavailable):
        raise
    except (httpx.HTTPError, OSError, TimeoutError):
        raise PreviewUnavailable("network") from None
    return b"".join(chunks)


def artifact_path(user_id: str, record_id: str) -> str:
    """Storage path for one avatar preview, scoped to its owner."""
    return f"{user_id}/{AVATARS_DIR}/{record_id}.png"


def build_photo_avatar_blob_store(settings: Settings) -> BlobStore:
    if settings.photo_avatar_blob_account_url:
        return AzureBlobStore(
            settings.photo_avatar_blob_account_url, settings.photo_avatar_blob_container,
        )
    return InMemoryBlobStore()


class PhotoAvatarArtifactStore:
    """Owner-scoped preview bytes; paths are composed from the caller's own id."""

    def __init__(self, blob: BlobStore) -> None:
        self._blob = blob

    async def put(self, user_id: str, record_id: str, data: bytes) -> None:
        await self._blob.put(artifact_path(user_id, record_id), data, PREVIEW_CONTENT_TYPE)

    async def get(self, user_id: str, record_id: str) -> bytes:
        return await self._blob.get(artifact_path(user_id, record_id))

    async def delete(self, user_id: str, record_id: str) -> None:
        # The exact path is the prefix; record ids are fixed-length hex, so no
        # other record's preview can share it.
        await self._blob.delete_prefix(artifact_path(user_id, record_id))

    async def close(self) -> None:
        await self._blob.close()
