"""Automation adapter over the shared policy, publication and tool builders."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from ..agents.agent_catalog import AgentCatalog
from ..agents.capabilities import SharedCapabilities, build_shared_capabilities
from ..agents.consent_service import describe_contracts, environment_hash
from ..agents.synthetic_governance import synthetic_spec
from ..agents.tool_exec import CHAT_ONLY_SYNTHETIC_TOOL_NAMES
from ..agents.tools import ToolRisk, is_safe_tool_name
from ..auth.base import AuthenticatedUser
from ..library.models import DocumentStatus
from ..policy.models import EffectivePolicy, PolicyRequest
from ..publishing.models import AssetVersionRef, ToolBundle
from ..publishing.service import PublicationService
from ..request_constraints import automatic_memory_allowed, tools_allowed
from ..websearch.contracts import WEBIQ_TOOL_NAMES
from .automation_common import AutomationError, AutomationModel, ExecutionLimits, digest, json_bytes
from .automation_models import FrozenWorkflow, WorkflowCheckpoint
from .models import Workflow


class WorkflowSelection(AutomationModel):
    name: str | None = Field(default=None, min_length=1, max_length=32)
    publishedSource: AssetVersionRef | None = None
    model: str = Field(min_length=1, max_length=128)
    region: str | None = Field(default=None, max_length=64)
    dataZone: str | None = Field(default=None, max_length=64)
    documentIds: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def one_source(self) -> WorkflowSelection:
        if (self.name is None) == (self.publishedSource is None):
            raise ValueError("Select one owned workflow or one exact published workflow version.")
        if self.publishedSource is not None and self.publishedSource.kind != "workflow":
            raise ValueError("The published source must be a workflow.")
        if len(set(self.documentIds)) != len(self.documentIds):
            raise ValueError("Document identifiers must be unique.")
        return self


@dataclass
class ResolvedSurface:
    contracts: dict[str, str]
    destinations: dict[str, str | None]
    schemas: SharedCapabilities


class WorkflowAccess:
    def __init__(self, state: Any, publications: PublicationService | None = None) -> None:
        self.state = state
        self.publications = publications

    async def actor(self, owner: str, user: AuthenticatedUser | None) -> EffectivePolicy:
        return (
            await self.state.policy.resolve(user, expected_owner=owner)
            if user is not None else await self.state.policy.resolve_unattended(owner)
        )

    async def private_agents(self, owner: str, workflow: Workflow) -> AgentCatalog:
        # Management reads are strict; the chat catalog's availability fallback
        # must not silently substitute a curated persona for an unavailable draft.
        owned = {agent.name: agent.to_spec() for agent in await self.state.agent_service.list_for(owner)}
        result = []
        for name in dict.fromkeys(step.agent for step in workflow.steps):
            target = self.state.agents.get(name) or owned.get(name)
            if target is None or not target.enabled or target.links:
                raise AutomationError("workflow_unavailable", "A workflow step is unavailable or recursive.")
            result.append(target.model_copy(deep=True))
        return AgentCatalog(agents=result)

    async def documents(self, owner: str, identifiers: list[str]) -> dict[str, str]:
        if not identifiers:
            return {}
        library = getattr(self.state, "document_library", None)
        if library is None:
            raise AutomationError("resources_unavailable", "The selected document library is unavailable.")
        result = {}
        for identifier in identifiers:
            document = await library.get_document(owner, identifier)
            if document.userId != owner or document.status != DocumentStatus.ready:
                raise AutomationError("context_revoked", "A selected owned document is no longer ready.")
            result[identifier] = digest(document.model_dump(mode="json"))
        return result

    def capabilities(
        self, owner: str, session_id: str, names: list[str], documents: list[str], *,
        nonce: str, safe_only: bool,
    ) -> SharedCapabilities:
        built = build_shared_capabilities(
            attached_tool_names=names, user_id=owner, session_id=session_id,
            nonce=nonce, allowed_document_ids=set(documents), library_tools_enabled=bool(documents),
            retrieval=getattr(self.state, "document_retrieval", None),
            web_search=getattr(self.state, "web_search", None),
            memory=getattr(self.state, "memory", None),
        )
        if built.unavailable:
            raise AutomationError("tool_unavailable", "A selected workflow capability is unavailable.")
        offered = {}
        for schema in built.tools:
            function = schema.get("function") or {}
            name = function.get("name")
            if not isinstance(name, str) or name not in built.handlers or synthetic_spec(name) is None:
                raise AutomationError("tool_unavailable", "The resolved tool surface is incomplete.")
            spec = synthetic_spec(name)
            if safe_only and spec is not None and spec.risk is not ToolRisk.safe:
                if name in names or name not in WEBIQ_TOOL_NAMES:
                    raise AutomationError("not_safe", "A requested tool is not safe for scheduled execution.")
                continue
            offered[name] = schema
        if set(built.handlers) != {item["function"]["name"] for item in built.tools}:
            raise AutomationError("tool_unavailable", "A workflow handler has no matching governed schema.")
        return SharedCapabilities(
            tools=list(offered.values()),
            handlers={name: built.handlers[name] for name in offered}, unavailable={},
        )

    async def surface(
        self, actor: EffectivePolicy, workflow: Workflow, agents: AgentCatalog, index: int, *,
        session_id: str, documents: list[str], nonce: str, safe_only: bool,
    ) -> ResolvedSurface:
        step = workflow.steps[index]
        target = agents.get(step.agent)
        if target is None or not target.enabled or target.links:
            raise AutomationError("workflow_unavailable", "A workflow step is unavailable or recursive.")
        names = list(dict.fromkeys([*target.tools, *step.extraTools]))
        if not tools_allowed():
            return ResolvedSurface({}, {}, SharedCapabilities(tools=[], handlers={}, unavailable={}))
        if any(name in CHAT_ONLY_SYNTHETIC_TOOL_NAMES for name in names):
            raise AutomationError("not_workflow_compatible", "A selected tool is chat-only or recursive.")
        built = self.capabilities(
            actor.owner_id, session_id, names, documents, nonce=nonce, safe_only=safe_only,
        )
        described = await describe_contracts(
            self.state, user_id=actor.owner_id, tool_names=names,
            schemas=built.tools, publication_metadata=True,
        )
        present = {item.canonical_name for item in described.values()}
        if set(names) - present:
            raise AutomationError("tool_unavailable", "A selected tool has no executable current contract.")
        destinations = {}
        for alias, item in described.items():
            spec = item.spec
            if not is_safe_tool_name(alias) or item.canonical_name in {"run_workflow", "delegate_to_agent"}:
                raise AutomationError("not_workflow_compatible", "A resolved tool is recursive or unclassified.")
            if safe_only and (
                spec.risk is not ToolRisk.safe or spec.needs_approval
                or spec.scopes or spec.secret_refs or spec.egress_allowlist
            ):
                raise AutomationError("not_safe", "Every scheduled tool must be a governed read-only operation.")
            await self.state.policy.require(actor, PolicyRequest(
                "tool.invoke", tool_name=item.canonical_name, tool_contract_digest=item.digest,
                resource_ids=tuple(documents),
            ))
            endpoint = item.metadata.get("endpoint")
            if isinstance(endpoint, str):
                destinations[alias] = endpoint
            elif alias in WEBIQ_TOOL_NAMES:
                destinations[alias] = self.state.settings.webiq_base_url or "https://api.microsoft.ai/v3"
            elif len(spec.egress_allowlist) == 1:
                destinations[alias] = "https://" + next(iter(spec.egress_allowlist))
            elif spec.risk is ToolRisk.destructive:
                destinations[alias] = "owner:" + actor.owner_id
            else:
                destinations[alias] = None
        return ResolvedSurface(
            {alias: item.digest for alias, item in described.items()}, destinations, built,
        )

    async def freeze(
        self, user: AuthenticatedUser, selection: WorkflowSelection, limits: ExecutionLimits, *,
        session_id: str, safe_only: bool,
    ) -> FrozenWorkflow:
        owner = user.internal_user_id
        actor = await self.actor(owner, None if safe_only else user)
        source: dict[str, Any]
        profile_digest = None
        profile: ToolBundle | None = None
        if selection.publishedSource is not None:
            if safe_only:
                raise AutomationError(
                    "reauthentication_required", "Published execution requires a current interactive consumer.",
                )
            if self.publications is None:
                raise AutomationError("publication_unavailable", "Publication execution is unavailable.", status=503)
            resolved = await self.publications.resolve_for_execution(
                actor, selection.publishedSource, mode="workflow",
            )
            if not isinstance(resolved.version.source, Workflow):
                raise AutomationError("workflow_unavailable", "The published version is not a workflow.")
            workflow = resolved.version.source
            agents = await self.publications.dependency_catalog(actor, resolved.version)
            source = {"mode": "published", "reference": selection.publishedSource.model_dump(mode="json")}
            profile_digest = resolved.version.profiles["workflow"].digest
            profile = resolved.version.profiles["workflow"]
        else:
            workflow = await self.state.workflow_service.get(owner, selection.name)
            if workflow is None or not workflow.enabled or workflow.userId != owner:
                raise AutomationError("workflow_unavailable", "The owned workflow is unavailable.", status=404)
            agents = await self.private_agents(owner, workflow)
            source = {
                "mode": "owned", "ownerId": owner, "name": workflow.name,
                "revision": workflow.revision, "incarnation": workflow.incarnation,
                "digest": digest(workflow.model_dump(mode="json")),
                "agentsDigest": digest(agents.model_dump(mode="json")),
            }
        deployment = self.state.catalog.resolve_deployment(
            selection.model, region=selection.region, data_zone=selection.dataZone, policy_filter=False,
        )
        entry = self.state.catalog.get(selection.model)
        if entry is None or deployment is None or not entry.supportsTools:
            raise AutomationError("model_unavailable", "Select an available tool-capable catalog model.", status=422)
        await self.state.policy.require(actor, PolicyRequest(
            "model.invoke", model_id=selection.model, deployment=deployment,
        ))
        if limits.maxRuntimeSeconds > self.state.settings.durable_workflow_timeout_seconds:
            raise AutomationError("unsupported_limit", "The run exceeds the operator's runtime bound.", status=422)
        resources = await self.documents(owner, selection.documentIds)
        nonce = secrets.token_hex(8)
        surfaces = [
            await self.surface(
                actor, workflow, agents, index, session_id=session_id,
                documents=selection.documentIds, nonce=nonce, safe_only=safe_only,
            )
            for index in range(len(workflow.steps))
        ]
        contracts = {key: value for surface in surfaces for key, value in surface.contracts.items()}
        subsets = [
            PublicationService.validate_subset(
                profile, f"step:{index}", surface.contracts,
                empty_document_scope=not selection.documentIds,
                tools_disabled=not tools_allowed(),
            )
            for index, surface in enumerate(surfaces)
        ] if profile is not None else []
        identity = digest({
            "workflow": workflow.model_dump(mode="json"), "agents": agents.model_dump(mode="json"),
            "source": source, "contracts": [surface.contracts for surface in surfaces],
            "documents": resources, "model": selection.model, "deployment": deployment.model_dump(mode="json"),
            "safe": safe_only,
            "subsets": [subset.model_dump(mode="json") for subset in subsets],
        })
        bundle = FrozenWorkflow(
            executionOwnerId=owner, workflow=workflow, agents=agents, source=source,
            modelId=selection.model, deployment=deployment, api=entry.api,
            bundleDigest=identity, approvedBundleDigest=profile_digest or identity,
            effectiveSubsets=subsets,
            environmentDigest=environment_hash(self.state), toolContracts=contracts,
            stepContracts=[surface.contracts for surface in surfaces],
            toolDestinations={key: value for surface in surfaces for key, value in surface.destinations.items()},
            selectedDocuments=list(selection.documentIds), memoryStamp=None,
            resourceStamps=resources, safeOnly=safe_only, nonce=nonce,
        )
        json_bytes(bundle.model_dump(mode="json"), limit=192 * 1024)
        await self.recheck(bundle, user=None if safe_only else user)
        return bundle

    async def recheck(self, bundle: FrozenWorkflow, *, user: AuthenticatedUser | None) -> EffectivePolicy:
        owner = bundle.executionOwnerId
        actor = await self.actor(owner, user)
        if environment_hash(self.state) != bundle.environmentDigest:
            raise AutomationError("policy_revoked", "Workflow feature or resource configuration changed.")
        await self.state.policy.require(actor, PolicyRequest(
            "model.invoke", model_id=bundle.modelId, deployment=bundle.deployment,
        ))
        if bundle.source["mode"] == "owned":
            current = await self.state.workflow_service.get(owner, bundle.workflow.name)
            if (
                current is None or not current.enabled
                or digest(current.model_dump(mode="json")) != bundle.source["digest"]
                or digest((await self.private_agents(owner, current)).model_dump(mode="json"))
                != bundle.source["agentsDigest"]
            ):
                raise AutomationError("policy_revoked", "The owned workflow or a step agent changed.")
        else:
            if self.publications is None:
                raise AutomationError("publication_unavailable", "Publication execution is unavailable.", status=503)
            await self.publications.recheck_execution(
                actor, AssetVersionRef.model_validate(bundle.source["reference"]), mode="workflow",
                implemented_bundle_digest=bundle.approvedBundleDigest,
                model_id=bundle.modelId, deployment=bundle.deployment,
            )
            resolved = await self.publications.resolve_for_execution(
                actor, AssetVersionRef.model_validate(bundle.source["reference"]), mode="workflow",
            )
            if len(bundle.effectiveSubsets) != len(bundle.stepContracts):
                raise AutomationError("state_corrupt", "The persisted publication subsets are incomplete.")
            for index, contracts in enumerate(bundle.stepContracts):
                current = self.publications.validate_subset(
                    resolved.version.profiles["workflow"], f"step:{index}", contracts,
                    empty_document_scope=not bundle.selectedDocuments,
                    tools_disabled=not tools_allowed(),
                )
                if current != bundle.effectiveSubsets[index]:
                    raise AutomationError("policy_revoked", "The approved effective tool subset changed.")
        if await self.documents(owner, bundle.selectedDocuments) != bundle.resourceStamps:
            raise AutomationError("context_revoked", "A selected document's access or version changed.")
        entitlement = await self.state.entitlements.get_for_admission(owner)
        decision = await self.state.entitlements.check_limits(
            owner, entitlement, failure_unavailable=True,
        )
        if not decision.allowed:
            raise AutomationError(
                "entitlement_denied", "Current account or usage policy does not allow execution.",
                status=decision.code,
            )
        return actor

    async def check_context(self, state: WorkflowCheckpoint) -> None:
        session = await self.state.session_repo.get_session(state.userId, state.sessionId)
        if state.bundle is None or (
            session.deletionEpoch != state.deletionEpoch
            or session.libraryDocumentIds != state.bundle.selectedDocuments
        ):
            raise AutomationError("context_revoked", "The conversation's effective resource scope changed.")
        if state.memoryContext is not None:
            if not automatic_memory_allowed():
                raise AutomationError("context_revoked", "Current request restrictions withhold remembered context.")
            await self.state.memory.validate_context_references(
                state.userId, state.memoryContext.preference, state.memoryContext.references,
            )

    @staticmethod
    def destination(bundle: FrozenWorkflow, tool: str, arguments: dict[str, Any]) -> str | None:
        destination = bundle.toolDestinations.get(tool)
        if tool == "browse_url":
            value = arguments.get("url")
            if not isinstance(value, str):
                raise AutomationError("arguments_not_reviewable", "The destination URL is missing.")
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise AutomationError("arguments_not_reviewable", "The destination must be public HTTPS.")
            # The configured provider AND the page being requested are material.
            destination = (destination or "") + " -> " + value
        return destination
