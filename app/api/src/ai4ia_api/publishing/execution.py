"""Run-bound publication checks and truthful approved/effective subset evidence."""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any

from ..catalog import DeploymentOption
from ..policy.context import (
    bind_publication_check, current_binding, publication_scope,
)
from ..policy.models import EffectivePolicy, PolicyDecision, PolicyError, PolicyRequest
from .models import (
    EffectiveSubset, PublicationError, PublicationExecutionMode, ResolvedPublication,
)
from .refs import AssetVersionRef, PublicationEvidence


@dataclass
class PublicationExecution:
    state: Any
    actor: EffectivePolicy
    resolved: ResolvedPublication
    mode: PublicationExecutionMode
    model_id: str
    deployment: DeploymentOption
    session_id: str | None
    document_scope: tuple[str, ...] | None
    scope: str = "root"
    actual: EffectiveSubset | None = None
    tools_disabled: bool = False
    expected_subset_digest: str | None = None

    @property
    def ref(self) -> AssetVersionRef:
        return self.resolved.version.reference()

    @property
    def skill_excluded(self) -> bool:
        return self.resolved.version.skillMode == "excluded"

    def derive(self, scope: str) -> PublicationExecution:
        return replace(self, scope=scope, actual=None)

    async def verify(self) -> None:
        if self.session_id is not None:
            session = await self.state.session_repo.get_session(self.actor.owner_id, self.session_id)
            scope = tuple(session.libraryDocumentIds) if session.libraryDocumentIds is not None else None
            if scope != self.document_scope:
                raise PublicationError("publication_document_scope_changed")
        bundle = self.resolved.version.profiles[self.mode]
        await self.state.publications.recheck_execution(
            self.actor, self.ref, mode=self.mode, implemented_bundle_digest=bundle.digest,
            model_id=self.model_id, deployment=self.deployment,
        )
        requirements = bundle.requirements.get(self.scope)
        if requirements is not None:
            for tool in bundle.tools:
                if tool.alias in requirements.required:
                    await self.state.policy.require(self.actor, PolicyRequest(
                        "tool.invoke", tool_name=tool.name, tool_contract_digest=tool.contractDigest,
                    ))

    async def observe(self, contracts: Mapping[str, str], offered: Sequence[str]) -> None:
        if set(offered) != contracts.keys():
            raise PublicationError("publication_ungoverned_offer")
        await self.verify()
        subset = self.state.publications.validate_subset(
            self.resolved.version.profiles[self.mode], self.scope, dict(contracts),
            empty_document_scope=self.document_scope == (),
            tools_disabled=self.tools_disabled,
        )
        if self.actual is not None and self.actual != subset:
            raise PublicationError("publication_effective_subset_changed")
        if self.expected_subset_digest is not None and subset.effectiveDigest != self.expected_subset_digest:
            raise PublicationError("publication_approval_subset_changed")
        self.actual = subset

    def check_request(self, request: PolicyRequest) -> None:
        from ..agents.consent_service import environment_hash

        if environment_hash(self.state) != self.resolved.version.profiles[self.mode].environmentDigest:
            raise PublicationError("publication_environment_changed")
        if request.operation == "model.invoke":
            if not any(
                binding.modelId == request.model_id and binding.option == request.deployment
                for binding in self.resolved.version.modelBindings
            ):
                raise PublicationError("publication_model_not_reviewed")
        if request.operation == "tool.invoke" and request.tool_contract_digest is not None:
            if self.actual is None:
                raise PublicationError("publication_offer_not_bound")
            if not any(
                tool.name == request.tool_name
                and self.actual.contracts.get(tool.alias) == request.tool_contract_digest
                for tool in self.resolved.version.profiles[self.mode].tools
            ):
                raise PublicationError("publication_contract_changed")

    def evidence(self) -> PublicationEvidence:
        bundle = self.resolved.version.profiles[self.mode]
        return PublicationEvidence(
            source=self.ref, approvedProfileDigest=bundle.digest,
            effectiveSubsetDigest=self.actual.effectiveDigest if self.actual is not None else None,
            approvalDigest=self.resolved.approval.digest, mode=self.mode, scope=self.scope,
            narrowing=tuple(self.actual.narrowing) if self.actual is not None else (),
            exclusions=tuple(bundle.exclusions),
        )


_current: ContextVar[PublicationExecution | None] = ContextVar("publication_execution", default=None)


def clear_publication_execution() -> None:
    _current.set(None)


def current_execution() -> PublicationExecution | None:
    return _current.get()


def publication_evidence() -> PublicationEvidence | None:
    current = _current.get()
    return current.evidence() if current is not None else None


def skill_loader_excluded() -> bool:
    current = _current.get()
    return current is not None and current.skill_excluded


async def prepare_execution(
    state: Any, ref: AssetVersionRef, *, mode: PublicationExecutionMode,
    model_id: str, deployment: DeploymentOption, session=None, scope: str = "root",
    tools_disabled: bool = False,
) -> PublicationExecution:
    binding = current_binding()
    if binding is None or binding.service is not state.policy:
        raise PolicyError(PolicyDecision("unavailable", "reauthentication_required"))
    actor = await binding.resolve()
    resolved = await state.publications.resolve_for_execution(actor, ref, mode=mode)
    execution = PublicationExecution(
        state=state, actor=actor, resolved=resolved, mode=mode,
        model_id=model_id, deployment=deployment,
        session_id=session.id if session is not None else None,
        document_scope=(
            tuple(session.libraryDocumentIds)
            if session is not None and session.libraryDocumentIds is not None else None
        ),
        scope=scope,
        tools_disabled=tools_disabled,
    )
    await execution.verify()
    return execution


def bind_execution(execution: PublicationExecution) -> None:
    _current.set(execution)
    bind_publication_check(execution.verify)


@contextmanager
def execution_scope(execution: PublicationExecution | None) -> Iterator[None]:
    token = _current.set(execution)
    try:
        if execution is not None:
            with publication_scope(execution.verify):
                yield
        else:
            yield
    finally:
        _current.reset(token)


async def observe_publication_offers(
    contracts: Mapping[str, str], offered: Sequence[str],
) -> None:
    current = _current.get()
    if current is not None:
        await current.observe(contracts, offered)


def check_publication_request(request: PolicyRequest) -> None:
    current = _current.get()
    if current is not None:
        current.check_request(request)
