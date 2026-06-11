"""Domain models for the per-user document library (Phase 11A).

Forward-looking but stable: the manifest carries the fields the later sub-phases
populate (Content Understanding summary, blob artifact paths, chunk count) so
adding them is never a breaking migration. 11A only writes the identity/dedupe
fields; the rest keep their inert defaults until ingest (11B) fills them.

Sharing is enabled in Phase 11F: ``visibility`` may be ``shared`` (read access to
the principals in ``acl``, by email) or ``public`` (every authenticated user in
the tenant). Mutations remain owner-only, and annotations/memories never travel
with a shared document. The fields are additive with inert defaults, so enabling
sharing was not a schema change.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


# userId stamped on the built-in analyzers. A real user id is never this value,
# so built-ins can never collide with or be mutated by a user's custom analyzers.
SYSTEM_OWNER = "__system__"


class DocumentStatus(str, Enum):
    # Manifest created; bytes not yet persisted.
    pending = "pending"
    # Raw bytes stored (blob); not yet cracked by Content Understanding.
    stored = "stored"
    # Content Understanding ingest in flight.
    analyzing = "analyzing"
    # Cracked + indexed; available for retrieval.
    ready = "ready"
    # Ingest failed; ``error`` carries the reason.
    failed = "failed"


class Modality(str, Enum):
    document = "document"
    image = "image"
    audio = "audio"
    video = "video"
    text = "text"
    other = "other"


class Visibility(str, Enum):
    # Owner-only (the default).
    private = "private"
    # Owner plus the principals listed in ``acl`` (Phase 11F sharing).
    shared = "shared"
    # Owner plus every authenticated user. The app authenticates against a single
    # Entra tenant (and dev auth is local), so "public" is tenant-walled by
    # construction: there is no unauthenticated path to a document. No public,
    # unauthenticated URLs are ever minted.
    public = "public"


class AnalyzerKind(str, Enum):
    # A Content Understanding prebuilt analyzer surfaced to every user.
    builtin = "builtin"
    # A user-defined analyzer stored in the per-user registry.
    custom = "custom"


class Analyzer(BaseModel):
    """A selectable Content Understanding analyzer.

    Built-ins are constants (see :data:`BUILTIN_ANALYZERS`) returned to every
    user and never persisted. Custom analyzers are stored per-user (PK
    ``/userId``) and selectable at upload. 11A only manages the registry; the CU
    config is consumed by the ingest worker in 11B.
    """

    id: str = Field(default_factory=_new_id)
    userId: str
    name: str
    description: str = ""
    kind: AnalyzerKind = AnalyzerKind.custom
    # Modalities this analyzer is appropriate for (UI filtering + validation).
    modalities: list[Modality] = Field(default_factory=lambda: [Modality.document])
    # CU base analyzer id this one derives from (e.g. a prebuilt). Used by the
    # 11B ingest worker; unset for a from-scratch custom analyzer.
    baseAnalyzerId: str | None = None
    # Opaque CU analyzer configuration (field schema, etc.). Validated/consumed
    # in 11B; kept as a free-form dict so the registry isn't coupled to the CU
    # schema version.
    config: dict = Field(default_factory=dict)
    createdAt: datetime = Field(default_factory=_now)
    updatedAt: datetime = Field(default_factory=_now)

    @property
    def builtin(self) -> bool:
        return self.kind == AnalyzerKind.builtin


# Built-in analyzers surfaced to every user. Logical descriptors only in 11A —
# the concrete CU prebuilt-analyzer ids + field configs are wired in 11B against
# the verified Content Understanding api-version, so nothing unverified is
# hardcoded here. ``id`` is stable and safe to reference from the manifest.
BUILTIN_ANALYZERS: tuple[Analyzer, ...] = (
    Analyzer(
        id="builtin-document",
        userId=SYSTEM_OWNER,
        name="Document",
        description="General document understanding (PDF, Office, text): layout, "
        "sections, tables, and a summary.",
        kind=AnalyzerKind.builtin,
        modalities=[Modality.document, Modality.text],
    ),
    Analyzer(
        id="builtin-image",
        userId=SYSTEM_OWNER,
        name="Image",
        description="Image understanding: caption, OCR text, and detected content.",
        kind=AnalyzerKind.builtin,
        modalities=[Modality.image],
    ),
    Analyzer(
        id="builtin-audio",
        userId=SYSTEM_OWNER,
        name="Audio",
        description="Audio transcription with speaker diarization.",
        kind=AnalyzerKind.builtin,
        modalities=[Modality.audio],
    ),
    Analyzer(
        id="builtin-video",
        userId=SYSTEM_OWNER,
        name="Video",
        description="Video understanding: transcript, key frames, and segments.",
        kind=AnalyzerKind.builtin,
        modalities=[Modality.video],
    ),
)

BUILTIN_ANALYZER_IDS: frozenset[str] = frozenset(a.id for a in BUILTIN_ANALYZERS)


class DocumentAnnotation(BaseModel):
    """An owner's note attached to a library document (Phase 11E-2).

    Annotations are additive, owner-private metadata: free-form notes the owner
    pins to a document, optionally anchored to a location (a page label, an
    ``mm:ss`` timestamp, a quoted span, etc.). They live on the manifest like
    :class:`DocumentVersion`, default empty so adding them is not a breaking
    migration, and round-trip through both the in-memory and Cosmos stores
    unchanged. They are presentation metadata for the owner only — never injected
    into the model's retrieval context — so they carry no prompt-injection surface.
    Mutations are owner-only (``require_owner``); reads use ``can_access`` (which is
    owner-only in v1).
    """

    id: str = Field(default_factory=_new_id)
    # The note text (multi-line allowed). Sanitized + length-capped at the API edge.
    body: str
    # Optional free-form anchor (single line): a page label, "mm:ss", a short quote.
    anchor: str = ""
    createdAt: datetime = Field(default_factory=_now)
    updatedAt: datetime = Field(default_factory=_now)

    def touch(self) -> None:
        self.updatedAt = _now()


class DocumentVersion(BaseModel):
    """An "adjust & return" derived artifact of a library document (Phase 11C).

    Each export writes a **new** versioned blob under
    ``{userId}/{documentId}/versions/{n}/...`` and appends one of these to the
    owning :class:`UserDocument`. The original artifacts (raw + parsed) are never
    overwritten, so the source stays immutable and every adjustment is an additive
    pointer. ``n`` is 1-based and dense (the manifest's next version is
    ``len(versions) + 1``). The field is additive with an inert default so adding
    it is not a breaking manifest migration and round-trips through both the
    in-memory and Cosmos stores unchanged.
    """

    n: int
    # Stored blob path of this version's artifact (under the versions/ prefix).
    path: str
    # Sanitized filename surfaced to the user/UI (already ``_safe_filename``-d).
    filename: str
    contentType: str = "text/markdown"
    size: int = 0
    # Short, single-line provenance (e.g. "totals by quarter"); sanitized before
    # it is ever shown to the model or the user.
    note: str = ""
    createdAt: datetime = Field(default_factory=_now)


class UserDocument(BaseModel):
    """A document in a user's cross-session library (manifest; PK ``/userId``).

    The raw bytes and any parsed artifacts live in blob storage (added with the
    11B ingest path); this manifest holds identity, status, dedupe hash, and the
    artifact pointers. ``userId`` is the partition key *and* is ownership-checked
    on every access (defense in depth).
    """

    id: str = Field(default_factory=_new_id)
    userId: str
    filename: str
    contentType: str = ""
    # Size of the original uploaded bytes.
    size: int = 0
    # sha256 of the raw bytes (hex). Combined with ``analyzerId`` it is the
    # dedupe key: re-uploading identical bytes for the same analyzer reuses the
    # existing manifest instead of re-cracking.
    contentHash: str = ""
    modality: Modality = Modality.other
    status: DocumentStatus = DocumentStatus.pending
    # Analyzer selected/used to crack this document (a builtin or custom id).
    analyzerId: str | None = None
    # One-line summary produced by Content Understanding (filled in 11B).
    summary: str = ""
    # Blob artifact paths (filled by the 11B ingest path): raw upload, parsed
    # markdown, and the chunk sidecar. Kept as nullable pointers so the manifest
    # is valid the moment it is created, before any artifact exists.
    rawPath: str | None = None
    parsedPath: str | None = None
    chunksPath: str | None = None
    # Number of embedded chunks indexed for retrieval (filled in 11B).
    chunkCount: int = 0
    # Failure reason when ``status == failed``.
    error: str | None = None
    # --- Sharing (Phase 11F) ---
    # ``visibility`` governs who may *read* this document; mutations always stay
    # owner-only. ``acl`` is the grant list for ``visibility == shared``: it holds
    # normalized grantee *emails* (lowercased), not internal user ids — sharing is
    # done by email (the universal collaboration primitive) while ownership and
    # partitioning stay keyed on the owner's ``userId``. Both fields are additive
    # with inert defaults (``private`` / empty), so enabling sharing is not a
    # breaking manifest migration. Annotations and saved memories never travel
    # with a shared document (owner-private by design).
    visibility: Visibility = Visibility.private
    acl: list[str] = Field(default_factory=list)
    # Sessions that reference this library document (for cascade/usage views).
    sessionLinks: list[str] = Field(default_factory=list)
    # "Adjust & return" derived artifacts (Phase 11C). Additive, default empty so
    # the manifest contract is unchanged for documents that were never exported.
    # The original raw/parsed artifacts are never mutated; each export appends one
    # dense, 1-based entry here.
    versions: list[DocumentVersion] = Field(default_factory=list)
    # Owner-private notes pinned to this document (Phase 11E-2). Additive, default
    # empty so the manifest contract is unchanged for un-annotated documents, and
    # deliberately excluded from the model-facing summary so notes never leak into
    # the retrieval/prompt context.
    annotations: list[DocumentAnnotation] = Field(default_factory=list)
    createdAt: datetime = Field(default_factory=_now)
    updatedAt: datetime = Field(default_factory=_now)

    def touch(self) -> None:
        self.updatedAt = _now()

    @property
    def version_count(self) -> int:
        return len(self.versions)

    @property
    def next_version(self) -> int:
        """1-based number for the next export (dense; one past the current max)."""
        return (max((v.n for v in self.versions), default=0)) + 1

    @property
    def latest_version(self) -> DocumentVersion | None:
        if not self.versions:
            return None
        return max(self.versions, key=lambda v: v.n)
