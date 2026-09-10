"""Owner-only publication changes and consent-scoped independent review."""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, Field

from ..auth.admin import evaluate_admin
from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..policy.models import PolicyOperation, PolicyRequest
from ..publishing.models import (
    ActivationRequest, AssetKind, AssetVersionRef, PublicationError, PublicationHead,
    PublicationSubmit, PublicationVersion, ReviewRequest, WithdrawalRequest,
)
from ..publishing.service import PublicationService

router = APIRouter(prefix="/api", tags=["publications"])


class PublicationCapabilities(BaseModel):
    enabled: bool = False
    actions: list[str] = Field(default_factory=list)
    operatorReviewAvailable: bool = False


class PublicationSummary(BaseModel):
    source: AssetVersionRef
    handle: str
    displayName: str
    description: str
    visibility: str
    modes: list[str]
    modelIds: list[str]
    skillMode: str


class PublicationCatalog(BaseModel):
    items: list[PublicationSummary]
    truncated: bool = False


class OwnerPublicationList(BaseModel):
    items: list[PublicationHead]
    truncated: bool = False


class OwnerPublicationState(PublicationHead):
    pendingSource: AssetVersionRef | None = None
    pendingDraftRevision: int | None = None
    activeSource: AssetVersionRef | None = None
    reviewDecision: Literal["approved", "rejected"] | None = None
    reviewerId: str | None = None


class ReviewStatus(BaseModel):
    reviewDecision: Literal["approved", "rejected"] | None = None
    reviewerId: str | None = None


class ReviewSummary(ReviewStatus):
    source: AssetVersionRef
    displayName: str
    headRevision: int


class ReviewList(BaseModel):
    items: list[ReviewSummary]
    truncated: bool = False


class ReviewDetail(ReviewStatus):
    version: PublicationVersion
    headRevision: int


def _service(request: Request) -> PublicationService:
    return request.app.state.publications


def _operator(request: Request, user: AuthenticatedUser) -> bool:
    return evaluate_admin(user, request.app.state.settings, request.headers.get("X-Admin-Secret"))


async def _review_status(service: PublicationService, source: AssetVersionRef) -> ReviewStatus:
    try:
        review = await service._review(source)
    except PublicationError as exc:
        if exc.reason != "publication_not_reviewed":
            raise
        return ReviewStatus()
    return ReviewStatus(reviewDecision=review.decision, reviewerId=review.reviewerId)


async def _owner_state(service: PublicationService, head: PublicationHead) -> OwnerPublicationState:
    pending = await service.head_reference(head, pending=True) if head.pendingVersion is not None else None
    active = await service.head_reference(head) if head.activeVersion is not None else None
    pending_revision = None
    review = ReviewStatus()
    if pending is not None:
        _, version = await service._version(pending)
        pending_revision = version.source.revision
        review = await _review_status(service, pending)
    return OwnerPublicationState(
        **head.model_dump(), pendingSource=pending, pendingDraftRevision=pending_revision,
        activeSource=active, **review.model_dump(),
    )


@router.get("/publications/capabilities", response_model=PublicationCapabilities)
async def publication_capabilities(
    request: Request, user: AuthenticatedUser = Depends(get_current_user),
) -> PublicationCapabilities:
    service = _service(request)
    if not service.enabled:
        return PublicationCapabilities()
    actor = await request.app.state.policy.resolve(user)
    actions = []
    operations: tuple[PolicyOperation, ...] = (
        "publication.submit", "publication.review", "publication.consume",
    )
    for operation in operations:
        decision = await request.app.state.policy.authorize(actor, PolicyRequest(operation))
        if decision.allowed:
            actions.append(operation.split(".", 1)[1])
    return PublicationCapabilities(
        enabled=True, actions=actions, operatorReviewAvailable=_operator(request, user),
    )


@router.get("/publications", response_model=PublicationCatalog)
async def list_publications(
    kind: AssetKind, request: Request, user: AuthenticatedUser = Depends(get_current_user),
) -> PublicationCatalog:
    service = _service(request)
    actor = await request.app.state.policy.resolve(user)
    heads = await service.catalog(actor, kind)
    items = []
    for head in heads[:100]:
        ref = await service.head_reference(head)
        _, version = await service._version(ref)
        items.append(PublicationSummary(
            source=ref, handle=head.handle, displayName=version.source.displayName,
            description=version.source.description, visibility=head.visibility.value,
            modes=list(version.profiles),
            modelIds=list(dict.fromkeys(item.modelId for item in version.modelBindings)),
            skillMode=version.skillMode,
        ))
    return PublicationCatalog(items=items, truncated=len(heads) > 100)


@router.get("/publications/mine", response_model=OwnerPublicationList)
async def list_my_publications(
    kind: AssetKind, request: Request, user: AuthenticatedUser = Depends(get_current_user),
) -> OwnerPublicationList:
    heads = await _service(request).owner_heads(await request.app.state.policy.resolve(user), kind)
    return OwnerPublicationList(items=heads[:100], truncated=len(heads) > 100)


@router.get("/publications/{kind}/{name}", response_model=OwnerPublicationState | None)
async def get_my_publication(
    kind: AssetKind, name: str, request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> OwnerPublicationState | None:
    service = _service(request)
    head = await service.owner_head(await request.app.state.policy.resolve(user), kind, name)
    if head is None:
        return None
    return await _owner_state(service, head)


@router.post("/publications/{kind}/{name}/submit", response_model=OwnerPublicationState)
async def submit_publication(
    kind: AssetKind, name: str, payload: PublicationSubmit, request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> OwnerPublicationState:
    service = _service(request)
    head = await service.submit(
        await request.app.state.policy.resolve(user), kind, name, payload,
    )
    return await _owner_state(service, head)


@router.post("/publications/{kind}/{name}/activate", response_model=OwnerPublicationState)
async def activate_publication(
    kind: AssetKind, name: str, payload: ActivationRequest, request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> OwnerPublicationState:
    service = _service(request)
    head = await service.activate(
        await request.app.state.policy.resolve(user), kind, name, payload,
    )
    return await _owner_state(service, head)


@router.post("/publications/{kind}/{name}/withdraw", response_model=OwnerPublicationState)
async def withdraw_publication(
    kind: AssetKind, name: str, payload: WithdrawalRequest, request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> OwnerPublicationState:
    service = _service(request)
    head = await service.withdraw(
        await request.app.state.policy.resolve(user), kind, name, payload.expectedHeadRevision,
    )
    return await _owner_state(service, head)


@router.get("/publication-reviews", response_model=ReviewList)
async def publication_reviews(
    kind: AssetKind, request: Request, user: AuthenticatedUser = Depends(get_current_user),
) -> ReviewList:
    service = _service(request)
    actor = await request.app.state.policy.resolve(user)
    heads = await service.review_inbox(actor, kind, operator_authorized=_operator(request, user))
    items = []
    for head in heads[:100]:
        ref = await service.head_reference(head, pending=True)
        _, version, _ = await service.review_source(actor, ref, operator_authorized=_operator(request, user))
        status = await _review_status(service, ref)
        items.append(ReviewSummary(
            source=ref, displayName=version.source.displayName, headRevision=head.revision,
            **status.model_dump(),
        ))
    return ReviewList(items=items, truncated=len(heads) > 100)


@router.get("/publication-reviews/{kind}/{owner}/{asset}/{version}", response_model=ReviewDetail)
async def publication_review_detail(
    kind: AssetKind, owner: Annotated[str, Path(min_length=1, max_length=256)],
    asset: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
    version: Annotated[int, Path(ge=1, le=20)],
    digest: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")], request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ReviewDetail:
    source = AssetVersionRef(kind=kind, ownerId=owner, assetId=asset, version=version, digest=digest)
    service = _service(request)
    head, snapshot, _ = await service.review_source(
        await request.app.state.policy.resolve(user), source, operator_authorized=_operator(request, user),
    )
    status = await _review_status(service, source)
    return ReviewDetail(version=snapshot, headRevision=head.revision, **status.model_dump())


@router.post("/publication-reviews/decision", response_model=ReviewSummary)
async def decide_publication_review(
    payload: ReviewRequest, request: Request, user: AuthenticatedUser = Depends(get_current_user),
) -> ReviewSummary:
    service = _service(request)
    head = await service.decide_review(
        await request.app.state.policy.resolve(user), payload,
        operator_authorized=_operator(request, user),
    )
    _, version = await service._version(payload.source)
    review = await service._review(payload.source)
    return ReviewSummary(
        source=payload.source, displayName=version.source.displayName, headRevision=head.revision,
        reviewDecision=review.decision, reviewerId=review.reviewerId,
    )
