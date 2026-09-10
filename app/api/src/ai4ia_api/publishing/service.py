"""Owner-consented review, immutable activation and current-consumer resolution."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from ..agents.agent_catalog import AgentCatalog, AgentSpec
from ..agents.user_agents import NAME_RE, UserAgent
from ..catalog import DeploymentOption
from ..library.access import can_access, normalize_principal
from ..library.models import Visibility
from ..policy.models import EffectivePolicy, PolicyDecision, PolicyError, PolicyRequest
from ..workflows.models import Workflow
from ..workflows.record_types import (
    AGENT_DEFINITION_KIND, CONTROL_RECORD_PREFIX, PUBLICATION_BUDGET_KIND,
    PUBLICATION_HEAD_KIND, WORKFLOW_DEFINITION_KIND, is_definition,
)
from .compiler import PublicationCompiler
from .models import (
    MAX_PUBLICATION_BYTES, MAX_VERSIONS_PER_ASSET, MAX_VERSIONS_PER_OWNER,
    ActivationRequest, AssetKind, AssetVersionRef, PublicationError, PublicationExecutionMode,
    PublicationHead, PublicationSubmit, PublicationVersion, ResolvedPublication,
    EffectiveSubset, NarrowingReason, ReviewDecision, ReviewRequest, ToolBundle, exact_digest,
)
from .refs import publication_handle
from .store import RecordQuery, RecordSnapshot, RecordStore, publication_head_id

_BUDGET_ID = f"{CONTROL_RECORD_PREFIX}publication:budget:v1"


def _version_id(asset: str, version: int) -> str:
    return f"{CONTROL_RECORD_PREFIX}publication:version:{asset}:{version}"


def _review_id(asset: str, version: int) -> str:
    return f"{CONTROL_RECORD_PREFIX}publication:review:{asset}:{version}"


class PublicationService:
    def __init__(self, state: Any, *, agents: RecordStore, workflows: RecordStore) -> None:
        self.state = state
        self._stores = {"agent": agents, "workflow": workflows}
        self.compiler = PublicationCompiler(state, self)

    @property
    def enabled(self) -> bool:
        return bool(self.state.settings.asset_publishing_enabled and self.state.policy.enabled)

    def _store(self, kind: AssetKind) -> RecordStore:
        return self._stores[kind]

    async def _actor(
        self, actor: EffectivePolicy, operation: str | None = None,
    ) -> EffectivePolicy:
        if not self.enabled:
            raise PublicationError("publication_disabled", 404)
        if actor.user is None or actor.user.provider != "entra":
            raise PolicyError(PolicyDecision("unavailable", "reauthentication_required"))
        fresh = await self.state.policy.resolve(actor.user, expected_owner=actor.owner_id)
        identity = self.state.policy.identity_decision(fresh)
        if identity is not None:
            raise PolicyError(identity)
        # Owner status/withdrawal are not contingent on retaining a publish grant.
        if operation is not None:
            request = PolicyRequest(
                "publication.submit" if operation == "submit" else
                "publication.review" if operation == "review" else "publication.consume",
            )
            await self.state.policy.require(fresh, request)
        return fresh

    @staticmethod
    def _tenant(actor: EffectivePolicy) -> str:
        if actor.user is None or not actor.user.tenant_id:
            raise PolicyError(PolicyDecision("unavailable", "reauthentication_required"))
        return actor.user.tenant_id

    async def _definition(
        self, owner: str, kind: AssetKind, name: str,
    ) -> tuple[RecordSnapshot, UserAgent | Workflow]:
        if not NAME_RE.fullmatch(name):
            raise PublicationError("publication_not_found", 404)
        snapshot = await self._store(kind).read(owner, name)
        definition_kind = AGENT_DEFINITION_KIND if kind == "agent" else WORKFLOW_DEFINITION_KIND
        if snapshot is None or not is_definition(
            snapshot.body, user_id=owner, kind=definition_kind, name=name,
        ):
            raise PublicationError("publication_not_found", 404)
        source = (
            UserAgent.model_validate(snapshot.body) if kind == "agent"
            else Workflow.model_validate(snapshot.body)
        )
        return snapshot, source

    @staticmethod
    def _head(snapshot: RecordSnapshot | None) -> PublicationHead:
        if snapshot is None:
            raise PublicationError("publication_not_found", 404)
        try:
            return PublicationHead.model_validate(snapshot.body)
        except ValidationError as exc:
            raise PublicationError("publication_unavailable", 503) from exc

    async def _version(self, ref: AssetVersionRef) -> tuple[RecordSnapshot, PublicationVersion]:
        snapshot = await self._store(ref.kind).read(
            ref.ownerId, _version_id(ref.assetId, ref.version),
        )
        if snapshot is None:
            raise PublicationError("publication_not_found", 404)
        try:
            version = PublicationVersion.model_validate(snapshot.body)
        except ValidationError as exc:
            raise PublicationError("publication_unavailable", 503) from exc
        if (
            version.reference() != ref or version.computed_digest() != ref.digest
            or version.source.userId != ref.ownerId
            or version.sourceDigest != exact_digest(version.source.model_dump(mode="json"))
            or (ref.kind == "agent") != isinstance(version.source, UserAgent)
        ):
            raise PublicationError("publication_version_changed")
        return snapshot, version

    async def _review(self, ref: AssetVersionRef) -> ReviewDecision:
        snapshot = await self._store(ref.kind).read(ref.ownerId, _review_id(ref.assetId, ref.version))
        if snapshot is None:
            raise PublicationError("publication_not_reviewed")
        review = ReviewDecision.model_validate(snapshot.body)
        if (
            review.source != ref or review.userId != ref.ownerId
            or review.reviewerId == ref.ownerId or review.digest != review.computed_digest()
        ):
            raise PublicationError("publication_unavailable", 503)
        return review

    async def submit(
        self, actor: EffectivePolicy, kind: AssetKind, name: str, request: PublicationSubmit,
    ) -> PublicationHead:
        actor = await self._actor(actor, "submit")
        owner, tenant = actor.owner_id, self._tenant(actor)
        records = self._store(kind)
        before, source = await self._definition(owner, kind, name)
        if source.revision != request.expectedRevision or not source.enabled:
            raise PublicationError("publication_source_changed")
        if request.reviewerUserId == owner:
            raise PublicationError("publication_independent_reviewer_required", 422)
        claims = actor.user.policy_claims if actor.user is not None else None
        if request.audience.groupAcl and (
            claims is None or not claims.groups_complete
            or set(request.audience.groupAcl) - set(claims.groups)
        ):
            raise PublicationError("publication_audience_not_authorized", 403)
        head_id = publication_head_id(name)
        old_head = await records.read(owner, head_id)
        previous = self._head(old_head) if old_head is not None else None
        same = previous is not None and not previous.deleted and previous.sourceIncarnation == source.incarnation
        head = previous if same else PublicationHead(
            id=head_id, userId=owner, tenantId=tenant, kind=kind, sourceName=name,
            sourceIncarnation=source.incarnation, assetId=uuid4().hex, handle="pending",
            revision=(previous.revision + 1) if previous is not None else 1,
            visibility=Visibility.private,
        )
        if head is None or head.versionCount >= MAX_VERSIONS_PER_ASSET:
            raise PublicationError("publication_version_limit")
        budget = await records.read(owner, _BUDGET_ID)
        count = budget.body.get("count") if budget is not None else 0
        if (
            isinstance(count, bool) or not isinstance(count, int)
            or not 0 <= count < MAX_VERSIONS_PER_OWNER
            or (budget is not None and budget.body.get("recordKind") != PUBLICATION_BUDGET_KIND)
        ):
            raise PublicationError("publication_owner_version_limit")
        models, profiles, dependencies = await self.compiler.compile(
            actor, source, model_ids=request.modelIds, modes=request.modes,
            skill_mode=request.skillMode,
        )
        number = head.versionCount + 1
        version = PublicationVersion(
            id=_version_id(head.assetId, number), userId=owner, tenantId=tenant,
            kind=kind, assetId=head.assetId, version=number, digest="0" * 64,
            source=source, sourceDigest=exact_digest(source.model_dump(mode="json")),
            audience=request.audience, modelBindings=models, profiles=dict(profiles),
            dependencies=dependencies, reviewConsent=request.reviewConsent,
            operatorReviewConsent=request.operatorReviewConsent,
            reviewerUserId=request.reviewerUserId, policyDigest=actor.digest,
            skillMode=request.skillMode,
        )
        version = version.model_copy(update={"digest": version.computed_digest()})
        # Reserve room for the bounded head/decision metadata at subsequent transitions.
        if len(version.model_dump_json().encode("utf-8")) > MAX_PUBLICATION_BYTES - 4096:
            raise PublicationError("publication_too_large", 422)
        head = head.model_copy(update={
            "handle": publication_handle(head.assetId), "versionCount": number,
            "pendingVersion": number, "reviewConsent": True,
            "operatorReviewConsent": request.operatorReviewConsent,
            "reviewerUserId": request.reviewerUserId,
            "revision": head.revision + 1 if same else head.revision,
        })
        await self._actor(actor, "submit")
        if not await records.atomic(
            owner, {name: before, head_id: old_head, version.id: None, _BUDGET_ID: budget},
            {
                head_id: head.model_dump(mode="json"), version.id: version.model_dump(mode="json"),
                _BUDGET_ID: {
                    "id": _BUDGET_ID, "userId": owner, "recordKind": PUBLICATION_BUDGET_KIND,
                    "count": count + 1,
                },
            },
        ):
            raise PublicationError("publication_source_changed")
        return head

    async def owner_heads(self, actor: EffectivePolicy, kind: AssetKind) -> list[PublicationHead]:
        actor = await self._actor(actor)
        rows = await self._store(kind).query(RecordQuery(
            PUBLICATION_HEAD_KIND, owner_id=actor.owner_id, limit=101,
        ))
        return [self._head(row) for row in rows]

    async def head_reference(
        self, head: PublicationHead, *, pending: bool = False,
    ) -> AssetVersionRef:
        number = head.pendingVersion if pending else head.activeVersion
        if number is None:
            raise PublicationError("publication_not_found", 404)
        row = await self._store(head.kind).read(head.userId, _version_id(head.assetId, number))
        if row is None:
            raise PublicationError("publication_unavailable", 503)
        reference = PublicationVersion.model_validate(row.body).reference()
        await self._version(reference)
        return reference

    async def owner_head(
        self, actor: EffectivePolicy, kind: AssetKind, name: str,
    ) -> PublicationHead | None:
        actor = await self._actor(actor)
        row = await self._store(kind).read(actor.owner_id, publication_head_id(name))
        if row is None:
            return None
        head = self._head(row)
        if head.tenantId != self._tenant(actor):
            raise PublicationError("publication_not_found", 404)
        return head

    async def catalog(
        self, actor: EffectivePolicy, kind: AssetKind, *, handle: str | None = None,
    ) -> list[PublicationHead]:
        actor = await self._actor(actor, "consume")
        claims = actor.user.policy_claims if actor.user is not None else None
        groups = claims.groups if claims is not None and claims.groups_complete else ()
        email = normalize_principal(actor.user.email if actor.user is not None else None)
        rows = await self._store(kind).query(RecordQuery(
            PUBLICATION_HEAD_KIND, tenant_id=self._tenant(actor), handle=handle,
            viewer_id=actor.owner_id, email=email, groups=groups, active_only=True, limit=101,
        ))
        return [
            head for row in rows if not (head := self._head(row)).deleted
            and head.activeVersion is not None
            and can_access(
                actor.owner_id, head, email=email, groups=groups, tenant_id=self._tenant(actor),
            )
        ]

    async def review_inbox(
        self, actor: EffectivePolicy, kind: AssetKind, *, operator_authorized: bool = False,
    ) -> list[PublicationHead]:
        actor = await self._actor(actor)
        role = await self.state.policy.authorize(actor, PolicyRequest("publication.review"))
        if not role.allowed and not operator_authorized:
            raise PublicationError("publication_review_not_authorized", 403)
        rows = await self._store(kind).query(RecordQuery(
            PUBLICATION_HEAD_KIND, tenant_id=self._tenant(actor), review_for=actor.owner_id,
            operator_only=not role.allowed, limit=101,
        ))
        return [self._head(row) for row in rows]

    async def review_source(
        self, actor: EffectivePolicy, ref: AssetVersionRef, *, operator_authorized: bool = False,
    ) -> tuple[PublicationHead, PublicationVersion, str]:
        actor = await self._actor(actor)
        _, version = await self._version(ref)
        head = self._head(await self._store(ref.kind).read(
            ref.ownerId, publication_head_id(version.source.name),
        ))
        if (
            head.deleted or head.assetId != ref.assetId or head.pendingVersion != ref.version
            or head.tenantId != self._tenant(actor) or version.tenantId != head.tenantId
            or not version.reviewConsent or actor.owner_id == ref.ownerId
            or (version.reviewerUserId is not None and version.reviewerUserId != actor.owner_id)
        ):
            raise PublicationError("publication_not_found", 404)
        role = await self.state.policy.authorize(actor, PolicyRequest("publication.review"))
        operator = operator_authorized and version.operatorReviewConsent
        if not role.allowed and not operator:
            raise PublicationError("publication_not_found", 404)
        return head, version, "reviewer" if role.allowed else "operator"

    async def _check_compilation(
        self, actor: EffectivePolicy, version: PublicationVersion, *, check_policy: bool = False,
    ) -> None:
        models, profiles, dependencies = await self.compiler.compile(
            actor, version.source,
            model_ids=list(dict.fromkeys(item.modelId for item in version.modelBindings)),
            modes=list(version.profiles), check_policy=check_policy,
            skill_mode=version.skillMode,
        )
        if (
            any(binding not in models for binding in version.modelBindings)
            or profiles != version.profiles
            or dependencies != version.dependencies
        ):
            raise PublicationError("publication_contract_changed")

    async def decide_review(
        self, actor: EffectivePolicy, request: ReviewRequest, *, operator_authorized: bool = False,
    ) -> PublicationHead:
        head, version, authority = await self.review_source(
            actor, request.source, operator_authorized=operator_authorized,
        )
        if head.revision != request.expectedHeadRevision:
            raise PublicationError("publication_version_changed")
        records = self._store(request.source.kind)
        head_before = await records.read(head.userId, head.id)
        source_before, source = await self._definition(head.userId, head.kind, head.sourceName)
        if exact_digest(source.model_dump(mode="json")) != version.sourceDigest:
            raise PublicationError("publication_source_changed")
        if request.decision == "approved":
            await self._check_compilation(actor, version)
        review = ReviewDecision(
            id=_review_id(head.assetId, version.version), userId=head.userId,
            tenantId=head.tenantId, source=request.source, decision=request.decision,
            reviewerId=actor.owner_id, authority="operator" if authority == "operator" else "reviewer",
            note=request.note, digest="0" * 64,
        )
        review = review.model_copy(update={"digest": review.computed_digest()})
        await self.review_source(actor, request.source, operator_authorized=operator_authorized)
        updated = head.model_copy(update={"revision": head.revision + 1})
        if (
            head_before is None or self._head(head_before) != head
            or not await records.atomic(
                head.userId,
                {head.id: head_before, head.sourceName: source_before, review.id: None},
                {head.id: updated.model_dump(mode="json"), review.id: review.model_dump(mode="json")},
            )
        ):
            raise PublicationError("publication_version_changed")
        return updated

    async def activate(
        self, actor: EffectivePolicy, kind: AssetKind, name: str, request: ActivationRequest,
    ) -> PublicationHead:
        actor = await self._actor(actor, "submit")
        if request.source.ownerId != actor.owner_id or request.source.kind != kind:
            raise PublicationError("publication_not_found", 404)
        _, version = await self._version(request.source)
        records = self._store(kind)
        head_before = await records.read(actor.owner_id, publication_head_id(name))
        head = self._head(head_before)
        source_before, source = await self._definition(actor.owner_id, kind, name)
        if (
            head.revision != request.expectedHeadRevision or head.deleted
            or head.assetId != request.source.assetId or head.pendingVersion != request.source.version
            or version.source.name != name or head.tenantId != self._tenant(actor)
            or source.incarnation != head.sourceIncarnation or not source.enabled
            or exact_digest(source.model_dump(mode="json")) != version.sourceDigest
        ):
            raise PublicationError("publication_source_changed")
        review = await self._review(request.source)
        if review.decision != "approved":
            raise PublicationError("publication_not_approved")
        await self._check_compilation(actor, version, check_policy=True)
        await self._actor(actor, "submit")
        updated = head.model_copy(update={
            **version.audience.model_dump(), "activeVersion": version.version,
            "pendingVersion": None, "revision": head.revision + 1,
        })
        if not await records.atomic(
            actor.owner_id, {name: source_before, head.id: head_before},
            {head.id: updated.model_dump(mode="json")},
        ):
            raise PublicationError("publication_source_changed")
        return updated

    async def withdraw(
        self, actor: EffectivePolicy, kind: AssetKind, name: str, expected_revision: int,
    ) -> PublicationHead:
        actor = await self._actor(actor)
        records = self._store(kind)
        before = await records.read(actor.owner_id, publication_head_id(name))
        head = self._head(before)
        if head.revision != expected_revision or head.tenantId != self._tenant(actor):
            raise PublicationError("publication_version_changed")
        updated = head.model_copy(update={
            "activeVersion": None, "pendingVersion": None, "visibility": Visibility.private,
            "acl": [], "groupAcl": [], "revision": head.revision + 1,
        })
        if not await records.atomic(actor.owner_id, {head.id: before}, {head.id: updated.model_dump(mode="json")}):
            raise PublicationError("publication_version_changed")
        return updated

    async def resolve_handle(
        self, actor: EffectivePolicy, kind: AssetKind, handle: str, *,
        mode: PublicationExecutionMode,
    ) -> ResolvedPublication | None:
        heads = await self.catalog(actor, kind, handle=handle)
        if not heads:
            return None
        if len(heads) != 1:
            raise PublicationError("publication_handle_conflict")
        head = heads[0]
        snapshot = await self._store(kind).read(
            head.userId, _version_id(head.assetId, head.activeVersion or 0),
        )
        if snapshot is None:
            raise PublicationError("publication_unavailable", 503)
        ref = PublicationVersion.model_validate(snapshot.body).reference()
        return await self.resolve_for_execution(actor, ref, mode=mode)

    async def resolve_for_execution(
        self, actor: EffectivePolicy, ref: AssetVersionRef, *,
        mode: PublicationExecutionMode, _seen: frozenset[str] = frozenset(),
    ) -> ResolvedPublication:
        actor = await self._actor(actor, "consume")
        marker = f"{ref.kind}:{ref.ownerId}:{ref.assetId}:{ref.version}"
        if marker in _seen or len(_seen) >= 16:
            raise PublicationError("publication_dependency_cycle")
        _, version = await self._version(ref)
        head = self._head(await self._store(ref.kind).read(
            ref.ownerId, publication_head_id(version.source.name),
        ))
        claims = actor.user.policy_claims if actor.user is not None else None
        if (
            head.deleted or head.assetId != ref.assetId or head.activeVersion != ref.version
            or version.tenantId != head.tenantId
            or not can_access(
                actor.owner_id, head, tenant_id=self._tenant(actor),
                email=actor.user.email if actor.user is not None else None,
                groups=claims.groups if claims is not None and claims.groups_complete else (),
            )
        ):
            raise PublicationError("publication_not_found", 404)
        _, current = await self._definition(ref.ownerId, ref.kind, version.source.name)
        if not current.enabled or current.incarnation != head.sourceIncarnation:
            raise PublicationError("publication_version_changed")
        if mode not in version.profiles:
            raise PublicationError("publication_mode_not_reviewed")
        review = await self._review(ref)
        if review.decision != "approved" or review.tenantId != version.tenantId:
            raise PublicationError("publication_not_approved")
        for dependency in version.dependencies:
            if dependency.published is not None:
                await self.resolve_for_execution(
                    actor, dependency.published,
                    mode="delegation" if ref.kind == "agent" else "workflow",
                    _seen=_seen | {marker},
                )
            else:
                current_agent = self.state.agents.get(dependency.name)
                if current_agent is None or not current_agent.enabled or exact_digest(
                    current_agent.model_dump(mode="json")
                ) != dependency.curatedDigest:
                    raise PublicationError("publication_dependency_changed")
        return ResolvedPublication(version=version, approval=review, headRevision=head.revision)

    async def recheck_execution(
        self, actor: EffectivePolicy, ref: AssetVersionRef, *,
        mode: PublicationExecutionMode, implemented_bundle_digest: str,
        model_id: str, deployment: DeploymentOption,
    ) -> None:
        resolved = await self.resolve_for_execution(actor, ref, mode=mode)
        version = resolved.version
        await self.state.policy.require(actor, PolicyRequest(
            "model.invoke", model_id=model_id, deployment=deployment,
        ))
        if not any(
            item.modelId == model_id and item.option == deployment for item in version.modelBindings
        ):
            raise PublicationError("publication_model_not_reviewed")
        await self._check_compilation(actor, version)
        if version.profiles[mode].digest != implemented_bundle_digest:
            raise PublicationError("publication_contract_changed")
        # Metadata discovery may have awaited a remote server. Re-read active
        # source/access after it so withdrawal during discovery cannot be lost.
        await self.resolve_for_execution(actor, ref, mode=mode)

    @staticmethod
    def agent_projection(resolved: ResolvedPublication, handle: str) -> AgentSpec:
        source = resolved.version.source
        if not isinstance(source, UserAgent):
            raise PublicationError("publication_kind_mismatch")
        return source.to_spec().model_copy(update={
            "name": handle, "sourceVersion": resolved.version.reference(),
            "publishedModes": list(resolved.version.profiles),
        })

    async def dependency_catalog(
        self, actor: EffectivePolicy, version: PublicationVersion,
    ) -> AgentCatalog:
        agents = []
        for dependency in version.dependencies:
            if dependency.published is not None:
                resolved = await self.resolve_for_execution(
                    actor, dependency.published,
                    mode="delegation" if version.kind == "agent" else "workflow",
                )
                agents.append(self.agent_projection(resolved, dependency.name))
            else:
                current = self.state.agents.get(dependency.name)
                if current is None or exact_digest(current.model_dump(mode="json")) != dependency.curatedDigest:
                    raise PublicationError("publication_dependency_changed")
                agents.append(current)
        return AgentCatalog(agents=agents)

    @staticmethod
    def validate_subset(
        bundle: ToolBundle, scope: str, contracts: dict[str, str], *,
        empty_document_scope: bool = False,
        tools_disabled: bool = False,
    ) -> EffectiveSubset:
        requirements = bundle.requirements.get(scope)
        if requirements is None:
            raise PublicationError("publication_mode_not_reviewed")
        expected = {tool.alias: tool.contractDigest for tool in bundle.tools}
        allowed = set(requirements.required) | requirements.optional.keys()
        if (
            contracts.keys() - allowed
            or any(expected.get(name) != digest for name, digest in contracts.items())
            or set(requirements.required) - contracts.keys()
        ):
            raise PublicationError("publication_contract_changed")
        removed = requirements.optional.keys() - contracts.keys()
        supported: set[NarrowingReason] = set()
        if empty_document_scope:
            supported.add("empty_document_scope")
        if tools_disabled:
            supported.add("request_tools_disabled")
        narrowing: list[NarrowingReason] = []
        for name in sorted(removed):
            reasons = set(requirements.optional[name]) & supported
            if not reasons:
                raise PublicationError("publication_optional_contract_unavailable", 503)
            narrowing.extend(reason for reason in sorted(reasons) if reason not in narrowing)
        return EffectiveSubset(
            profileDigest=bundle.digest, scope=scope, contracts=contracts,
            narrowing=narrowing, exclusions=bundle.exclusions,
            effectiveDigest=exact_digest({
                "profile": bundle.digest, "scope": scope, "contracts": contracts,
                "narrowing": narrowing, "exclusions": bundle.exclusions,
            }),
        )
