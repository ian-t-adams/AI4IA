"""Owner-only publication changes and consent-scoped independent review."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, Field

from ..auth.admin import evaluate_admin
from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..policy.models import PolicyOperation, PolicyRequest
from ..publishing.models import (
    ActivationRequest, AssetKind, AssetVersionRef, PublicationHead, PublicationSubmit,
    PublicationVersion, ReviewRequest, WithdrawalRequest,
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


class ReviewSummary(BaseModel):
    source: AssetVersionRef
    displayName: str
    headRevision: int


class ReviewList(BaseModel):
    items: list[ReviewSummary]
    truncated: bool = False


class ReviewDetail(BaseModel):
    version: PublicationVersion
    headRevision: int


def _service(request: Request) -> PublicationService:
    return request.app.state.publications


def _operator(request: Request, user: AuthenticatedUser) -> bool:
    return evaluate_admin(user, request.app.state.settings, request.headers.get("X-Admin-Secret"))


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


@router.get("/publications/{kind}/{name}", response_model=PublicationHead | None)
async def get_my_publication(
    kind: AssetKind, name: str, request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PublicationHead | None:
    return await _service(request).owner_head(await request.app.state.policy.resolve(user), kind, name)


@router.post("/publications/{kind}/{name}/submit", response_model=PublicationHead)
async def submit_publication(
    kind: AssetKind, name: str, payload: PublicationSubmit, request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PublicationHead:
    return await _service(request).submit(
        await request.app.state.policy.resolve(user), kind, name, payload,
    )


@router.post("/publications/{kind}/{name}/activate", response_model=PublicationHead)
async def activate_publication(
    kind: AssetKind, name: str, payload: ActivationRequest, request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PublicationHead:
    return await _service(request).activate(
        await request.app.state.policy.resolve(user), kind, name, payload,
    )


@router.post("/publications/{kind}/{name}/withdraw", response_model=PublicationHead)
async def withdraw_publication(
    kind: AssetKind, name: str, payload: WithdrawalRequest, request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PublicationHead:
    return await _service(request).withdraw(
        await request.app.state.policy.resolve(user), kind, name, payload.expectedHeadRevision,
    )


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
        items.append(ReviewSummary(
            source=ref, displayName=version.source.displayName, headRevision=head.revision,
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
    head, snapshot, _ = await _service(request).review_source(
        await request.app.state.policy.resolve(user), source, operator_authorized=_operator(request, user),
    )
    return ReviewDetail(version=snapshot, headRevision=head.revision)


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
    return ReviewSummary(
        source=payload.source, displayName=version.source.displayName, headRevision=head.revision,
    )
