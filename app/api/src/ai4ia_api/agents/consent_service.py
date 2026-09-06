"""Derive consent contracts from the same owned, enabled surfaces as execution."""
from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import Any

from ..conversations.policy import resolve_conversation_policy
from ..sessions.models import Session
from ..sessions.repository import SessionNotFoundError
from .agent_catalog import AgentCatalog
from .capabilities import capability_builder_for_state
from .consent import (
    ConsentAssessment,
    ConsentChecker,
    ConsentDecision,
    ConsentSnapshot,
    ConsentStatus,
    ToolConsentState,
    check_consent,
    contract_hash,
    tool_contract_hash,
)
from .mcp_execution import McpPlane, build_mcp_turn_tools_multi
from .mcp_servers import McpNotFoundError, UserMcpServer, is_mcp_tool_name
from .mcp_skills import build_load_skill_definition
from .synthetic_governance import synthetic_spec
from .tool_exec import ToolContext, ToolExecutor
from .tools import ToolRegistry

_CREDENTIAL_FIELD = re.compile(
    r"(?:^|_)(?:key|secret|password|credential|credentials|connection_string|authorization|token)$",
    re.I,
)
_SERVICES = (
    "web_search", "memory", "document_retrieval", "document_compute",
    "inline_attachment_analysis", "image_artifacts", "video_artifacts",
    "document_artifacts", "mcp_service", "official_mcp_service", "workflow_service",
)
logger = logging.getLogger(__name__)


def tool_auto_approve_available(state: Any) -> bool:
    settings = getattr(state, "settings", None)
    if getattr(settings, "tool_auto_approve_enabled", False) is not True:
        return False
    if getattr(settings, "env", None) == "local":
        return True
    return (
        getattr(settings, "auth_provider", None) == "entra"
        and bool(getattr(settings, "entra_tenant_id", None))
        and bool(getattr(settings, "entra_audience", None))
        and getattr(settings, "session_store", None) == "cosmos"
        and bool(getattr(settings, "cosmos_endpoint", None))
    )


def environment_hash(state: Any) -> str:
    settings = getattr(state, "settings", None)
    values = (
        settings.model_dump(mode="json")
        if settings is not None and hasattr(settings, "model_dump") else {}
    )
    # Persist only a digest of noncredential configuration. Destinations, feature
    # posture, resource scopes, allowlists and limits all invalidate old consent.
    configuration = {
        name: value for name, value in values.items()
        if not _CREDENTIAL_FIELD.search(name)
    }
    catalog = getattr(state, "catalog", None)
    return contract_hash({
        "settings": configuration,
        "services": {
            name: (
                getattr(state, name, None) is not None
                and getattr(getattr(state, name), "enabled", True) is not False
            )
            for name in _SERVICES
        },
        "models": [contract_hash(model) for model in getattr(catalog, "models", [])],
    })


def mcp_server_reader(
    service: Any, *, user_id: str, official: bool = False,
) -> Callable[[str], Awaitable[UserMcpServer | None]]:
    async def read(name: str) -> UserMcpServer | None:
        if official:
            return next(
                (server for server in await service.list_all() if server.name == name), None
            )
        try:
            return await service.get(user_id, name)
        except McpNotFoundError:
            return None
    return read


async def execution_tools_for_state(
    state: Any,
    *,
    user_id: str,
    tool_names: Sequence[str],
    ctx: ToolContext,
) -> tuple[ToolRegistry, ToolExecutor, ToolContext]:
    if not any(is_mcp_tool_name(name) for name in tool_names):
        return state.tool_registry, state.tool_executor, ctx
    planes: list[McpPlane] = []
    for attribute, official in (("official_mcp_service", True), ("mcp_service", False)):
        service = getattr(state, attribute, None)
        if service is None:
            continue
        servers = await service.list_all() if official else await service.list_for(user_id)
        planes.append(McpPlane(
            servers=servers, secrets=service, connector=service.connector,
            plane_id="official" if official else "default",
            resolver=service.resolver, health=service,
            current_server=mcp_server_reader(service, user_id=user_id, official=official),
        ))
    built = build_mcp_turn_tools_multi(
        planes=planes, attached_tool_names=tool_names,
        correlation_id=ctx.correlation_id, approval_policy=ctx.approval_policy,
        untrusted_context=ctx.untrusted_context,
        invocation_approvals=ctx.invocation_approvals,
        approval_sink=ctx.approval_sink, consent_checker=ctx.consent_checker,
    )
    if built is None:
        return state.tool_registry, state.tool_executor, ctx
    registry, executor, mcp_ctx = built
    return registry, executor, replace(
        mcp_ctx, granted_scopes=ctx.granted_scopes,
        approvals=mcp_ctx.approvals | ctx.approvals,
    )


async def _contracts(
    state: Any,
    *,
    user_id: str,
    tool_names: Sequence[str],
    schemas: Sequence[dict[str, Any]],
) -> dict[str, str]:
    registry, executor, ctx = await execution_tools_for_state(
        state, user_id=user_id, tool_names=tool_names, ctx=ToolContext()
    )
    names = [ctx.tool_aliases.get(name, name) for name in tool_names]
    result: dict[str, str] = {}
    for schema in executor.schema_for(
        names, registry=registry, ctx=ctx, consented_names=names,
    ):
        fn = schema["function"]
        definition = executor.get(fn["name"])
        spec = registry.get(fn["name"])
        if definition is not None and spec is not None:
            result[fn["name"]] = tool_contract_hash(
                spec, fn["parameters"], description=fn.get("description"),
                metadata=definition.consent_metadata,
            )
    for schema in schemas:
        fn = schema.get("function") or {}
        name = fn.get("name")
        if not isinstance(name, str):
            continue
        spec = synthetic_spec(name)
        if spec is not None and spec.enabled and not spec.scopes:
            result[name] = tool_contract_hash(
                spec, fn.get("parameters") or {}, description=fn.get("description"),
            )
    return result


async def _chat_schemas(
    state: Any, *, user_id: str, session: Session,
    tool_names: Sequence[str], email: str | None,
) -> list[dict[str, Any]]:
    schemas, _ = capability_builder_for_state(
        state, user_id=user_id, session_id=session.id, email=email,
        allowed_document_ids=(
            set(session.libraryDocumentIds) if session.libraryDocumentIds is not None else None
        ),
        nonce="consent",
    )(tool_names)
    compute = getattr(state, "document_compute", None)
    if compute is not None and session.libraryDocumentIds != []:
        extra, _ = compute.build_capability(
            user_id=user_id, session_id=session.id, nonce="consent", email=email,
            allowed_document_ids=(
                set(session.libraryDocumentIds) if session.libraryDocumentIds is not None else None
            ),
        )
        schemas.extend(extra)
    analysis = getattr(state, "inline_attachment_analysis", None)
    if analysis is not None:
        documents = await state.session_repo.list_documents(user_id, session.id)
        attachments = [
            {"id": doc.id, "filename": doc.filename} for doc in documents if doc.rawRef
        ]
        if attachments:
            extra, _ = analysis.build_capability(
                user_id=user_id, session_id=session.id, nonce="consent",
                attachments=attachments,
            )
            schemas.extend(extra)
    if "generate_image" in tool_names and getattr(state, "image_artifacts", None) is not None:
        from ..images.capability import build_image_capability
        from ..images.service import ImageGenerationService

        extra, _ = build_image_capability(
            image_service=ImageGenerationService(catalog=state.catalog, gateway=state.gateway),
            artifact_store=state.image_artifacts, entitlements=state.entitlements,
            metering=state.usage, catalog=state.catalog, user_id=user_id,
            session_id=session.id, sink=[], preferences=session.imagePreferences,
        )
        schemas.extend(extra)
    if "generate_video" in tool_names and getattr(state, "video_artifacts", None) is not None:
        from ..videos.capability import build_video_capability
        from ..videos.service import VideoGenerationService

        extra, _ = build_video_capability(
            video_service=VideoGenerationService(
                catalog=state.catalog, gateway=state.gateway,
                poll_interval_seconds=state.settings.gateway_video_poll_interval_seconds,
                max_wait_seconds=state.settings.gateway_video_max_wait_seconds,
            ),
            artifact_store=state.video_artifacts, entitlements=state.entitlements,
            metering=state.usage, catalog=state.catalog, user_id=user_id,
            session_id=session.id, sink=[],
        )
        schemas.extend(extra)
    if (
        "process_document" in tool_names and session.libraryDocumentIds != []
        and getattr(state, "document_artifacts", None) is not None
        and getattr(state, "document_retrieval", None) is not None
        and session.model is not None
    ):
        from ..docprocessing.capability import build_document_processing_capability
        from ..docprocessing.service import DocumentProcessingService

        deployment = state.catalog.resolve_deployment(session.model)
        if deployment is not None:
            extra, _ = build_document_processing_capability(
                processing_service=DocumentProcessingService(
                    retrieval=state.document_retrieval, gateway=state.gateway,
                    settings=state.settings,
                ),
                artifact_store=state.document_artifacts, entitlements=state.entitlements,
                metering=state.usage, deployment=deployment, model_id=session.model,
                user_id=user_id, session_id=session.id, settings=state.settings, sink=[],
                allowed_document_ids=(
                    set(session.libraryDocumentIds) if session.libraryDocumentIds is not None else None
                ),
            )
            schemas.extend(extra)
    return schemas


async def session_snapshot(
    state: Any, *, user_id: str, session: Session,
    email: str | None = None, explicit_agent: str | None = None,
) -> ConsentSnapshot:
    policy = await resolve_conversation_policy(
        state, user_id, session, explicit_agent=explicit_agent,
    )
    linked = {}
    if policy.agent is not None and policy.agent.links:
        catalog = await state.agent_service.catalog_for(user_id, state.agents)
        linked = {name: catalog.get(name) for name in policy.agent.links}
    schemas = await _chat_schemas(
        state, user_id=user_id, session=session, tool_names=policy.effective_tools,
        email=email,
    )
    contracts = await _contracts(
        state, user_id=user_id, tool_names=policy.effective_tools, schemas=schemas,
    )
    official = getattr(state, "official_mcp_service", None)
    if official is not None and (policy.agent is not None or policy.effective_tools):
        skill = build_load_skill_definition(servers=await official.list_all(), reader=official)
        if skill is not None:
            contracts[skill.spec.name] = tool_contract_hash(
                skill.spec, skill.parameters, metadata=skill.consent_metadata,
            )
    return ConsentSnapshot(
        selection_hash=contract_hash({
            "agent": policy.agent, "linked": linked, "tools": policy.effective_tools,
            "instructions": policy.instructions,
            "documents": session.libraryDocumentIds, "images": session.imagePreferences,
        }),
        environment_hash=environment_hash(state),
        contracts=contracts,
    )


def session_consent_checker(
    state: Any, *, user_id: str, session: Session,
    email: str | None = None, explicit_agent: str | None = None,
) -> ConsentChecker | None:
    initial = session.toolConsentState
    if initial is None:
        return None

    async def check(tool: str, implemented: str) -> ConsentDecision:
        if not tool_auto_approve_available(state):
            return ConsentDecision(consent_id=initial.grant.id, reason="consent_disabled")
        try:
            live = await state.session_repo.get_session(user_id, session.id)
        except SessionNotFoundError:
            return ConsentDecision(consent_id=initial.grant.id, reason="consent_revoked")
        consent = live.toolConsentState
        if (
            consent is None or consent.grant.id != initial.grant.id
            or consent.userId != user_id or consent.sessionId != session.id
            or consent.grant.scope != "session" or consent.runId is not None
        ):
            return ConsentDecision(consent_id=initial.grant.id, reason="consent_revoked")
        entitlement = await state.entitlements.check(user_id)
        if not entitlement.allowed:
            return ConsentDecision(consent_id=initial.grant.id, reason="entitlement_denied")
        snapshot = await session_snapshot(
            state, user_id=user_id, session=live, email=email, explicit_agent=explicit_agent,
        )
        # Discovery may await a remote catalog. Revocation must not be lost
        # merely because it landed during that await rather than before it.
        try:
            latest = await state.session_repo.get_session(user_id, session.id)
        except SessionNotFoundError:
            return ConsentDecision(consent_id=initial.grant.id, reason="consent_revoked")
        if latest.toolConsentState != consent:
            return ConsentDecision(consent_id=initial.grant.id, reason="consent_revoked")
        if (
            latest.agentName != live.agentName
            or latest.toolOverrides != live.toolOverrides
            or latest.systemPrompt != live.systemPrompt
            or latest.libraryDocumentIds != live.libraryDocumentIds
            or latest.imagePreferences != live.imagePreferences
            or environment_hash(state) != snapshot.environment_hash
        ):
            return ConsentDecision(consent_id=initial.grant.id, reason="consent_changed")
        if not tool_auto_approve_available(state):
            return ConsentDecision(consent_id=initial.grant.id, reason="consent_disabled")
        return check_consent(consent, snapshot, tool=tool, implemented_contract=implemented)

    return check


async def inspect_session_consent(
    state: Any, *, user_id: str, session_id: str, email: str | None = None,
) -> ConsentAssessment:
    """Report verified current authority, not a persisted opt-in checkbox."""
    session = await state.session_repo.get_session(user_id, session_id)
    consent = session.toolConsentState
    if consent is None:
        return ConsentAssessment(
            consent=session.toolConsent,
            status="revoked" if session.toolConsentVersion or session.toolConsent else "off",
        )
    if not tool_auto_approve_available(state):
        return ConsentAssessment(consent=consent.grant, status="disabled")
    checker = session_consent_checker(
        state, user_id=user_id, session=session, email=email,
    )
    if checker is None:
        return ConsentAssessment(consent=consent.grant, status="revoked")
    # One probe verifies the whole contract set. A zero-tool grant is still a
    # valid, empty scope; it cannot authorize a tool introduced after opt-in.
    name = next(iter(consent.contracts), "")
    try:
        decision = await checker(name, consent.contracts.get(name, ""))
    except Exception:  # A failed verification must never advertise active consent.
        logger.warning("Session tool consent status could not be verified.")
        return ConsentAssessment(consent=consent.grant, status="unavailable")
    statuses: dict[str, ConsentStatus] = {
        "consent_expired": "expired",
        "consent_revoked": "revoked",
        "consent_changed": "changed",
        "consent_disabled": "disabled",
        "entitlement_denied": "disabled",
    }
    if decision.approved or (
        decision.reason == "consent_not_granted" and not consent.contracts
    ):
        return ConsentAssessment(consent=consent.grant, active=True, status="active")
    return ConsentAssessment(
        consent=consent.grant, status=statuses.get(decision.reason or "", "unavailable"),
    )


async def workflow_snapshot(
    state: Any, *, user_id: str, session: Session, workflow: Any,
    composed: AgentCatalog, email: str | None = None,
) -> ConsentSnapshot:
    contracts: dict[str, str] = {}
    agents: dict[str, Any] = {}
    builder = capability_builder_for_state(
        state, user_id=user_id, session_id=session.id, email=email,
        allowed_document_ids=(
            set(session.libraryDocumentIds) if session.libraryDocumentIds is not None else None
        ),
        nonce="consent",
    )
    for step in workflow.steps:
        agent = composed.get(step.agent)
        agents[step.agent] = agent
        if agent is None or not agent.enabled:
            continue
        names = list(dict.fromkeys([*agent.tools, *step.extraTools]))
        schemas, _ = builder(names)
        contracts.update(await _contracts(
            state, user_id=user_id, tool_names=names, schemas=schemas,
        ))
    return ConsentSnapshot(
        selection_hash=contract_hash({
            "workflow": workflow, "agents": agents, "documents": session.libraryDocumentIds,
        }),
        environment_hash=environment_hash(state),
        contracts=contracts,
    )


def run_consent_checker(
    state: Any, *, consent: ToolConsentState | None, workflow: Any,
    user_id: str, run_id: str, session: Session,
    assistant_message_id: str, email: str | None = None,
) -> ConsentChecker:
    async def check(tool: str, implemented: str) -> ConsentDecision:
        consent_id = consent.grant.id if consent else None
        try:
            messages = await state.session_repo.list_messages(user_id, session.id)
        except SessionNotFoundError:
            return ConsentDecision(consent_id=consent_id, reason="consent_revoked")
        message = next((m for m in messages if m.id == assistant_message_id), None)
        if (
            message is None or message.workflowConsentRevoked
            or message.workflowRunId != run_id
            or message.workflowToolConsentState != consent
            or message.workflowRunStatus not in {"pending", "accepted", "running", "acceptance_unknown"}
        ):
            return ConsentDecision(consent_id=consent_id, reason="consent_revoked")
        entitlement = await state.entitlements.check(user_id)
        if not entitlement.allowed:
            return ConsentDecision(consent_id=consent_id, reason="entitlement_denied")
        if consent is None:
            return ConsentDecision()
        if (
            not tool_auto_approve_available(state)
            or consent.userId != user_id or consent.sessionId != session.id
            or consent.runId != run_id or consent.grant.scope != "run"
        ):
            return ConsentDecision(consent_id=consent_id, reason="consent_disabled")
        current_workflow = await state.workflow_service.get(user_id, workflow.name)
        if current_workflow is None or not current_workflow.enabled:
            return ConsentDecision(consent_id=consent_id, reason="consent_changed")
        composed = await state.agent_service.catalog_for(user_id, state.agents)
        snapshot = await workflow_snapshot(
            state, user_id=user_id, session=session, workflow=current_workflow,
            composed=composed, email=email,
        )
        try:
            latest_messages = await state.session_repo.list_messages(user_id, session.id)
        except SessionNotFoundError:
            return ConsentDecision(consent_id=consent_id, reason="consent_revoked")
        latest = next((m for m in latest_messages if m.id == assistant_message_id), None)
        if (
            latest is None or latest.workflowConsentRevoked
            or latest.workflowRunId != run_id or latest.workflowToolConsentState != consent
            or latest.workflowRunStatus not in {"pending", "accepted", "running", "acceptance_unknown"}
        ):
            return ConsentDecision(consent_id=consent_id, reason="consent_revoked")
        if not tool_auto_approve_available(state):
            return ConsentDecision(consent_id=consent_id, reason="consent_disabled")
        if environment_hash(state) != snapshot.environment_hash:
            return ConsentDecision(consent_id=consent_id, reason="consent_changed")
        return check_consent(consent, snapshot, tool=tool, implemented_contract=implemented)

    return check
