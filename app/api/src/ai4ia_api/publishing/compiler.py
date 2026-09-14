"""Compile reviewed source from the same runtime tool contracts as execution."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from ..agents.agent_catalog import AgentCatalog, AgentSpec
from ..agents.capabilities import capability_builder_for_state
from ..agents.consent import ToolContractDescription, contract_hash
from ..agents.consent_service import _chat_schemas, describe_contracts, environment_hash
from ..agents.mcp_servers import is_mcp_tool_name
from ..agents.mcp_skills import build_load_skill_definition, discover_skills
from ..agents.orchestration import build_delegate_capability
from ..agents.user_agents import UserAgent
from ..policy.models import EffectivePolicy, PolicyRequest
from ..sessions.models import Session
from ..workflows.capability import safe_workflow_capability_builder, workflow_tool_ineligible_reason
from ..workflows.models import Workflow
from .models import (
    BundleRequirements, DependencyBinding, PublishedModel, PublishedTool, PublicationError,
    NarrowingReason, ProfileExclusion, PublicationExecutionMode, SkillMode, ToolBundle, exact_digest,
)

if TYPE_CHECKING:
    from .service import PublicationService


def _offered(contract: ToolContractDescription) -> PublishedTool:
    exact_digest(dict(contract.parameters))
    exact_digest(dict(contract.metadata))
    resources = []
    for item in contract.metadata.get("skills", []):
        if isinstance(item, dict):
            resources.append({
                key: item.get(key) for key in ("name", "uri", "version", "mimeType", "server")
            })
    return PublishedTool(
        name=contract.canonical_name, alias=contract.spec.name,
        contractDigest=contract.digest,
        parametersDigest=contract_hash(contract.parameters),
        description=(contract.description or contract.spec.description)[:2000],
        descriptionTruncated=len(contract.description or contract.spec.description) > 2000,
        risk=contract.spec.risk.value, scopes=sorted(contract.spec.scopes),
        egress=sorted(contract.spec.egress_allowlist), resources=resources,
    )


class PublicationCompiler:
    def __init__(self, state: Any, publications: PublicationService) -> None:
        self.state = state
        self.publications = publications

    async def dependencies(
        self, actor: EffectivePolicy, source: UserAgent | Workflow,
    ) -> tuple[list[DependencyBinding], AgentCatalog]:
        names = source.links if isinstance(source, UserAgent) else [
            step.agent for step in source.steps
        ]
        bindings: list[DependencyBinding] = []
        agents: list[AgentSpec] = []
        for name in dict.fromkeys(names):
            curated = self.state.agents.get(name)
            if curated is not None and curated.enabled:
                bindings.append(DependencyBinding(
                    name=name, curatedDigest=exact_digest(curated.model_dump(mode="json")),
                ))
                agents.append(curated)
                continue
            resolved = await self.publications.resolve_handle(
                actor, "agent", name, mode="delegation" if isinstance(source, UserAgent) else "workflow",
            )
            if resolved is None or not isinstance(resolved.version.source, UserAgent):
                raise PublicationError("publication_private_reference_unsupported", 422)
            bindings.append(DependencyBinding(name=name, published=resolved.version.reference()))
            agents.append(resolved.version.source.to_spec().model_copy(update={
                "name": name, "sourceVersion": resolved.version.reference(),
            }))
        return bindings, AgentCatalog(agents=agents)

    async def models(
        self, actor: EffectivePolicy, ids: Sequence[str], *, check_policy: bool = True,
    ) -> list[PublishedModel]:
        bindings: list[PublishedModel] = []
        for identifier in ids:
            entry = self.state.catalog.get(identifier)
            if entry is None or getattr(entry, "runtimeEnabled", True) is not True:
                raise PublicationError("publication_model_unavailable", 422)
            options = self.state.catalog.eligible_options(entry, policy_filter=check_policy)
            for option in options:
                if check_policy and not self.state.policy.decide(actor, PolicyRequest(
                    "model.invoke", model_id=identifier, deployment=option,
                )).allowed:
                    continue
                if not option.modelVersion:
                    raise PublicationError("publication_model_version_unknown", 422)
                bindings.append(PublishedModel(
                    modelId=identifier, api=entry.api, category=entry.category, option=option,
                    requiredRealtimeProtocol=getattr(entry, "requiredRealtimeProtocol", None),
                    runtimeEnabled=getattr(entry, "runtimeEnabled", True),
                ))
            if not any(binding.modelId == identifier for binding in bindings):
                raise PublicationError("publication_model_unavailable", 422)
        return bindings

    async def _official_only(self, names: Sequence[str]) -> None:
        selected = [name for name in names if is_mcp_tool_name(name)]
        if not selected:
            return
        service = getattr(self.state, "official_mcp_service", None)
        servers = await service.list_all() if service is not None else []
        for name in selected:
            server_name, separator, tool_name = name.removeprefix("mcp:").partition("/")
            server = next((item for item in servers if separator and item.name == server_name), None)
            if server is None or not server.enabled or not any(
                tool.name == tool_name for tool in server.discoveredTools
            ):
                raise PublicationError("publication_private_reference_unsupported", 422)
            if server.lastError:
                raise PublicationError("publication_discovery_unavailable", 503)

    async def profile(
        self, actor: EffectivePolicy, source: UserAgent | Workflow, *,
        mode: PublicationExecutionMode, model_id: str, composed: AgentCatalog,
        skill_mode: SkillMode = "versioned",
    ) -> ToolBundle:
        state = self.state
        environment = environment_hash(state)
        descriptions: dict[str, ToolContractDescription] = {}
        requirements: dict[str, BundleRequirements] = {}
        session = Session(id="publication-profile", userId=actor.owner_id, model=model_id)
        email = actor.user.email if actor.user is not None else None
        if isinstance(source, UserAgent):
            if mode not in {"chat", "delegation", "workflow", "voice"}:
                raise PublicationError("publication_mode_not_supported", 422)
            agents = [(source.to_spec(), list(source.tools))]
        else:
            if mode not in {"workflow", "workflow_tool"}:
                raise PublicationError("publication_mode_not_supported", 422)
            if mode == "workflow_tool" and workflow_tool_ineligible_reason(
                source, composed=composed, registry=state.tool_registry,
            ) is not None:
                raise PublicationError("publication_mode_not_supported", 422)
            agents = []
            for step in source.steps:
                target = composed.get(step.agent)
                if target is None or target.links:
                    raise PublicationError("publication_dependency_unavailable", 422)
                agents.append((target, list(dict.fromkeys([*target.tools, *step.extraTools]))))
        for index, (agent, names) in enumerate(agents):
            if skill_mode == "excluded" and "load_skill" in names:
                raise PublicationError("publication_required_skill_excluded", 422)
            if mode == "voice":
                if any(state.tool_executor.get(name) is None for name in names):
                    raise PublicationError("publication_voice_required_tool_unavailable", 422)
                if names and not getattr(state.settings, "realtime_tools_enabled", False):
                    raise PublicationError("publication_voice_tools_unavailable", 422)
            if mode == "delegation" and any(
                state.tool_executor.get(name) is None for name in names
            ):
                raise PublicationError("publication_mode_not_supported", 422)
            await self._official_only(names)
            if mode == "chat":
                schemas = await _chat_schemas(
                    state, user_id=actor.owner_id, session=session, tool_names=names,
                    email=email, include_attachments=False,
                    publication_metadata=True,
                )
                if agent.links:
                    deployment = state.catalog.resolve_deployment(model_id, policy_filter=False)
                    if deployment is None:
                        raise PublicationError("publication_model_unavailable", 422)
                    extra, _, _ = build_delegate_capability(
                        orchestrator=agent, composed=composed, gateway=state.gateway,
                        registry=state.tool_registry, executor=state.tool_executor,
                        deployment=deployment.deploymentName,
                    )
                    schemas.extend(extra)
                if "run_workflow" in names:
                    # Its current schema embeds the caller's private workflow list.
                    raise PublicationError("publication_private_reference_unsupported", 422)
            elif mode in {"workflow", "workflow_tool"}:
                if agent.links:
                    raise PublicationError("publication_mode_not_supported", 422)
                builder = capability_builder_for_state(
                    state, user_id=actor.owner_id, session_id=session.id, email=email,
                    nonce="publication",
                )
                if mode == "workflow_tool":
                    builder = safe_workflow_capability_builder(builder)
                schemas, _ = builder(names)
            else:
                schemas = []
            current = await describe_contracts(
                state, user_id=actor.owner_id, tool_names=names, schemas=schemas,
                publication_metadata=True,
            )
            present = {item.canonical_name for item in current.values()}
            if set(names) - present:
                raise PublicationError("publication_tool_unavailable", 422)
            for alias, description in current.items():
                if alias in descriptions and descriptions[alias].digest != description.digest:
                    raise PublicationError("publication_conflicting_tool_contract", 422)
                descriptions[alias] = description
            optional: dict[str, list[NarrowingReason]] = {}
            for alias, contract in current.items():
                if contract.canonical_name not in names and contract.canonical_name != "delegate_to_agent":
                    reasons: list[NarrowingReason] = ["request_tools_disabled"]
                    if contract.canonical_name in {"fetch_document", "run_code", "export_document"}:
                        reasons.append("empty_document_scope")
                    optional[alias] = reasons
            requirements["root" if isinstance(source, UserAgent) else f"step:{index}"] = BundleRequirements(
                required=[alias for alias in current if alias not in optional],
                optional=optional,
            )
        if mode == "chat" and skill_mode == "versioned":
            official = getattr(state, "official_mcp_service", None)
            if official is not None:
                servers = await official.list_all()
                if any(server.lastError for server in servers if server.enabled):
                    raise PublicationError("publication_discovery_unavailable", 503)
                skills = discover_skills(servers)
                for skill in skills:
                    parsed = urlparse(skill.uri)
                    versions = parse_qs(parsed.query, keep_blank_values=True)
                    if (
                        skill.version is None or parsed.fragment or set(versions) != {"version"}
                        or versions["version"] != [skill.version]
                        or skill.version.lower() in {"default", "latest", "current"}
                    ):
                        raise PublicationError("publication_unversioned_skill", 422)
                definition = build_load_skill_definition(servers=servers, reader=official)
                if definition is not None:
                    descriptions[definition.spec.name] = ToolContractDescription(
                        definition.spec, definition.parameters, definition.spec.description,
                        definition.consent_metadata, definition.spec.name,
                    )
                    root = requirements["root"]
                    if isinstance(source, UserAgent) and "load_skill" in source.tools:
                        requirements["root"] = root.model_copy(update={
                            "required": [*root.required, definition.spec.name],
                        })
                    else:
                        requirements["root"] = root.model_copy(update={
                            "optional": {**root.optional, definition.spec.name: ["request_tools_disabled"]},
                        })
        if mode == "chat" and isinstance(source, UserAgent):
            for target in composed.agents:
                if any(state.tool_executor.get(name) is None for name in target.tools):
                    raise PublicationError("publication_delegation_tool_unsupported", 422)
                current = await describe_contracts(
                    state, user_id=actor.owner_id, tool_names=target.tools, schemas=[],
                    publication_metadata=True,
                )
                if set(target.tools) - {item.canonical_name for item in current.values()}:
                    raise PublicationError("publication_tool_unavailable", 422)
                for alias, description in current.items():
                    if alias in descriptions and descriptions[alias].digest != description.digest:
                        raise PublicationError("publication_conflicting_tool_contract", 422)
                    descriptions[alias] = description
                requirements[f"delegate:{target.name}"] = BundleRequirements(required=list(current))
        tools = [_offered(descriptions[name]) for name in sorted(descriptions)]
        exclusions: list[ProfileExclusion] = ["skills_excluded_by_author"] if skill_mode == "excluded" else []
        if environment_hash(state) != environment:
            raise PublicationError("publication_environment_changed")
        return ToolBundle(
            mode=mode, tools=tools, requirements=requirements, exclusions=exclusions,
            environmentDigest=environment,
            digest=exact_digest({
                "mode": mode, "tools": [tool.model_dump(mode="json") for tool in tools],
                "environment": environment,
                "requirements": {
                    key: requirement.model_dump(mode="json") for key, requirement in requirements.items()
                },
                "exclusions": exclusions,
            }),
        )

    async def compile(
        self, actor: EffectivePolicy, source: UserAgent | Workflow, *,
        model_ids: Sequence[str], modes: Sequence[PublicationExecutionMode],
        check_policy: bool = True,
        skill_mode: SkillMode = "versioned",
    ) -> tuple[list[PublishedModel], Mapping[PublicationExecutionMode, ToolBundle], list[DependencyBinding]]:
        dependencies, composed = await self.dependencies(actor, source)
        models = await self.models(actor, model_ids, check_policy=check_policy)
        if isinstance(source, UserAgent) and source.defaultModel is not None and source.defaultModel not in model_ids:
            raise PublicationError("publication_default_model_not_reviewed", 422)
        profiles: dict[PublicationExecutionMode, ToolBundle] = {}
        for mode in modes:
            profile = await self.profile(
                actor, source, mode=mode, model_id=model_ids[0], composed=composed,
                skill_mode=skill_mode,
            )
            for model_id in model_ids[1:]:
                other = await self.profile(
                    actor, source, mode=mode, model_id=model_id, composed=composed,
                    skill_mode=skill_mode,
                )
                if other.digest != profile.digest:
                    raise PublicationError("publication_model_specific_tool_contract", 422)
            profiles[mode] = profile
        return models, profiles, dependencies
