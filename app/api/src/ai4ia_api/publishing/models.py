"""Immutable publication input, source, review and active-head contracts."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..agents.user_agents import UserAgent
from ..auth.policy_claims import GROUP_ID
from ..catalog import DeploymentOption
from ..library.access import normalize_principal, valid_grantee_email
from ..library.models import Visibility
from ..workflows.models import Workflow
from ..workflows.record_types import (
    PUBLICATION_HEAD_KIND, PUBLICATION_REVIEW_KIND, PUBLICATION_VERSION_KIND,
)
from .refs import AssetVersionRef

AssetKind = Literal["agent", "workflow"]
PublicationExecutionMode = Literal["chat", "delegation", "workflow", "workflow_tool", "voice"]
MAX_PUBLICATION_BYTES = 128 * 1024
MAX_VERSIONS_PER_ASSET = 20
MAX_VERSIONS_PER_OWNER = 1000


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def exact_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode("ascii")
    if len(encoded) > MAX_PUBLICATION_BYTES:
        raise PublicationError("publication_too_large", 422)
    return hashlib.sha256(encoded).hexdigest()


class PublicationError(RuntimeError):
    def __init__(self, reason: str, code: int = 409) -> None:
        self.reason = reason
        self.code = code
        super().__init__(reason)


class PublicationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PublicationAudience(PublicationRecord):
    visibility: Visibility = Visibility.shared
    acl: list[str] = Field(default_factory=list, max_length=100)
    groupAcl: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("acl")
    @classmethod
    def emails(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(normalize_principal(value) for value in values))
        if any(not valid_grantee_email(value) or len(value) > 254 for value in cleaned):
            raise ValueError("Invalid publication grantee.")
        return cleaned

    @field_validator("groupAcl")
    @classmethod
    def groups(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(GROUP_ID.fullmatch(value) is None for value in values):
            raise ValueError("Publication groups must be exact unique object IDs.")
        return values

    @model_validator(mode="after")
    def coherent_audience(self) -> PublicationAudience:
        if self.visibility != Visibility.shared and (self.acl or self.groupAcl):
            raise ValueError("Only shared publication visibility has individual/group grantees.")
        return self


class PublicationSubmit(PublicationRecord):
    expectedRevision: int = Field(ge=0, strict=True)
    audience: PublicationAudience
    modelIds: list[str] = Field(min_length=1, max_length=32)
    modes: list[PublicationExecutionMode] = Field(min_length=1, max_length=5)
    reviewConsent: bool = Field(strict=True)
    operatorReviewConsent: bool = Field(default=False, strict=True)
    reviewerUserId: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def explicit_review(self) -> PublicationSubmit:
        if not self.reviewConsent or self.audience.visibility == Visibility.private:
            raise ValueError("Publishing requires explicit review consent and a shared audience.")
        if self.audience.visibility == Visibility.shared and not (self.audience.acl or self.audience.groupAcl):
            raise ValueError("A shared version requires at least one grantee.")
        if len(set(self.modelIds)) != len(self.modelIds) or len(set(self.modes)) != len(self.modes):
            raise ValueError("Duplicate publication model or execution mode.")
        return self


class PublishedTool(PublicationRecord):
    name: str = Field(min_length=1, max_length=256)
    alias: str = Field(min_length=1, max_length=64)
    contractDigest: str = Field(pattern=r"^[0-9a-f]{64}$")
    parametersDigest: str = Field(pattern=r"^[0-9a-f]{64}$")
    description: str = Field(default="", max_length=2000)
    risk: str = Field(max_length=32)
    scopes: list[str] = Field(default_factory=list, max_length=32)
    egress: list[str] = Field(default_factory=list, max_length=32)
    resources: list[dict[str, Any]] = Field(default_factory=list, max_length=32)


class ToolBundle(PublicationRecord):
    mode: PublicationExecutionMode
    tools: list[PublishedTool] = Field(default_factory=list, max_length=128)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PublishedModel(PublicationRecord):
    modelId: str = Field(min_length=1, max_length=128)
    api: str = Field(min_length=1, max_length=32)
    category: str = Field(min_length=1, max_length=64)
    option: DeploymentOption

    @model_validator(mode="after")
    def declared_version(self) -> PublishedModel:
        if not self.option.modelVersion:
            raise ValueError("Versioned publication requires a catalog-declared model version.")
        return self


class DependencyBinding(PublicationRecord):
    name: str = Field(min_length=1, max_length=32)
    published: AssetVersionRef | None = None
    curatedDigest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def one_source(self) -> DependencyBinding:
        if (self.published is None) == (self.curatedDigest is None):
            raise ValueError("A dependency must identify exactly one source.")
        return self


class PublicationVersion(PublicationRecord):
    id: str
    userId: str
    tenantId: str
    recordKind: Literal["ai4ia.publication.version.v1"] = PUBLICATION_VERSION_KIND
    kind: AssetKind
    assetId: str
    version: int = Field(ge=1, le=MAX_VERSIONS_PER_ASSET, strict=True)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: UserAgent | Workflow
    sourceDigest: str = Field(pattern=r"^[0-9a-f]{64}$")
    audience: PublicationAudience
    modelBindings: list[PublishedModel] = Field(min_length=1, max_length=128)
    profiles: dict[PublicationExecutionMode, ToolBundle] = Field(min_length=1, max_length=5)
    dependencies: list[DependencyBinding] = Field(default_factory=list, max_length=6)
    reviewConsent: bool
    operatorReviewConsent: bool = False
    reviewerUserId: str | None = None
    submittedAt: datetime = Field(default_factory=utc_now)
    policyDigest: str

    def reference(self) -> AssetVersionRef:
        return AssetVersionRef(
            kind=self.kind, ownerId=self.userId, assetId=self.assetId,
            version=self.version, digest=self.digest,
        )

    def computed_digest(self) -> str:
        return exact_digest(self.model_dump(mode="json", exclude={"digest"}))


class PublicationHead(PublicationAudience):
    id: str
    userId: str
    tenantId: str
    recordKind: Literal["ai4ia.publication.head.v1"] = PUBLICATION_HEAD_KIND
    kind: AssetKind
    sourceName: str
    sourceIncarnation: str | None = None
    assetId: str
    handle: str
    revision: int = Field(ge=1, strict=True)
    versionCount: int = Field(default=0, ge=0, le=MAX_VERSIONS_PER_ASSET, strict=True)
    activeVersion: int | None = None
    pendingVersion: int | None = None
    deleted: bool = False
    reviewConsent: bool = False
    operatorReviewConsent: bool = False
    reviewerUserId: str | None = None


class ReviewDecision(PublicationRecord):
    id: str
    userId: str
    tenantId: str
    recordKind: Literal["ai4ia.publication.review.v1"] = PUBLICATION_REVIEW_KIND
    source: AssetVersionRef
    decision: Literal["approved", "rejected"]
    reviewerId: str
    authority: Literal["reviewer", "operator"]
    reviewedAt: datetime = Field(default_factory=utc_now)
    note: str = Field(default="", max_length=1000)
    digest: str

    def computed_digest(self) -> str:
        return exact_digest(self.model_dump(mode="json", exclude={"digest"}))


class ReviewRequest(PublicationRecord):
    source: AssetVersionRef
    expectedHeadRevision: int = Field(ge=1, strict=True)
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=1000)


class ActivationRequest(PublicationRecord):
    source: AssetVersionRef
    expectedHeadRevision: int = Field(ge=1, strict=True)


class WithdrawalRequest(PublicationRecord):
    expectedHeadRevision: int = Field(ge=1, strict=True)


class ResolvedPublication(PublicationRecord):
    version: PublicationVersion
    approval: ReviewDecision
    headRevision: int
