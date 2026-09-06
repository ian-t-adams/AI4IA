"""Consent is not a capability, allowlist, scope grant or dispatch shortcut."""
from __future__ import annotations

from dataclasses import replace

import pytest

from ai4ia_api.agents.approvals import ApprovalPolicy
from ai4ia_api.agents.consent import (
    ConsentDecision, ConsentSnapshot, check_consent, contract_hash, mint_consent,
)
from ai4ia_api.agents.runtime import run_agent_turn
from ai4ia_api.agents.tool_exec import ToolContext, ToolDefinition, build_tools
from ai4ia_api.agents.tools import ToolRisk, ToolSpec
from tests.test_agent_runtime import ScriptedGateway, _assistant_text, _assistant_tool_call


@pytest.mark.parametrize("scopes", [frozenset(), frozenset({"send"})])
async def test_auto_approval_never_grants_missing_scopes(scopes):
    sent = []
    spec = ToolSpec(
        name="send", description="Send", risk=ToolRisk.external, scopes=frozenset({"send"}),
    )
    registry, executor = build_tools(extra=[ToolDefinition(
        spec=spec, parameters={"type": "object"}, handler=lambda args, ctx: sent.append(args),
    )])

    async def consent(_name, _contract):
        return ConsentDecision(approved=True, scope="session", consent_id="a" * 32)

    gateway = ScriptedGateway([
        _assistant_tool_call("c1", "send", "{}"), _assistant_text("done"),
    ])
    result = await run_agent_turn(
        deployment="m", messages=[{"role": "user", "content": "send"}], tool_names=["send"],
        gateway=gateway, registry=registry, executor=executor,
        ctx=ToolContext(granted_scopes=scopes, consent_checker=consent),
    )
    if scopes:
        assert sent == [{}]
        assert result.steps[0].approval == "session"
    else:
        assert not sent
        assert result.steps[0].detail == "missing_scopes"


@pytest.mark.parametrize("offered", [False, True])
async def test_even_valid_consent_cannot_dispatch_unoffered_tool(offered):
    sent = []
    registry, executor = build_tools(extra=[ToolDefinition(
        spec=ToolSpec(name="send", description="Send", risk=ToolRisk.external),
        parameters={"type": "object"}, handler=lambda args, ctx: sent.append(args),
    )])

    async def consent(_name, _contract):
        return ConsentDecision(approved=True, scope="session", consent_id="a" * 32)

    result = await run_agent_turn(
        deployment="m", messages=[{"role": "user", "content": "send"}],
        tool_names=["send"] if offered else [],
        gateway=ScriptedGateway([
            _assistant_tool_call("c1", "send", "{}"), _assistant_text("done"),
        ]),
        registry=registry, executor=executor, ctx=ToolContext(consent_checker=consent),
    )
    assert len(sent) == int(offered)
    if not offered:
        assert result.steps[0].detail == "not_offered"


async def test_operator_opt_out_is_not_recorded_as_user_consent():
    registry, executor = build_tools(extra=[ToolDefinition(
        spec=ToolSpec(name="send", description="Send", risk=ToolRisk.external),
        parameters={"type": "object"}, handler=lambda _args, _ctx: {"sent": True},
    )])
    result = await run_agent_turn(
        deployment="m", messages=[{"role": "user", "content": "send"}], tool_names=["send"],
        gateway=ScriptedGateway([
            _assistant_tool_call("c1", "send", "{}"), _assistant_text("done"),
        ]),
        registry=registry, executor=executor,
        ctx=replace(ToolContext(), approval_policy=ApprovalPolicy.off),
    )
    assert result.steps[0].approval == "operator"
    assert result.steps[0].consent_id is None


@pytest.mark.parametrize("arguments,allowed", [('{"to":"owner"}', True), ('{"to":42}', False)])
async def test_consented_calls_still_validate_arguments(arguments, allowed):
    dispatched = []
    registry, executor = build_tools(extra=[ToolDefinition(
        spec=ToolSpec(name="send", description="Send", risk=ToolRisk.external),
        parameters={
            "type": "object", "properties": {"to": {"type": "string"}}, "required": ["to"],
        },
        handler=lambda args, ctx: dispatched.append(args),
    )])

    async def consent(_name, _contract):
        return ConsentDecision(approved=True, scope="run", consent_id="b" * 32)

    result = await run_agent_turn(
        deployment="m", messages=[{"role": "user", "content": "send"}], tool_names=["send"],
        gateway=ScriptedGateway([
            _assistant_tool_call("c1", "send", arguments), _assistant_text("done"),
        ]),
        registry=registry, executor=executor, ctx=ToolContext(consent_checker=consent),
    )
    assert len(dispatched) == int(allowed)
    assert result.steps[0].approval == "run"
    if not allowed:
        assert result.steps[0].detail == "validation_error"


def test_adding_a_contract_requires_renewal_even_before_the_new_tool_is_called():
    snapshot = ConsentSnapshot(
        contract_hash("selection"), contract_hash("environment"), {"existing": "a" * 64},
    )
    consent = mint_consent(snapshot, user_id="user", session_id="session")
    assert check_consent(
        consent, snapshot, tool="existing", implemented_contract="a" * 64,
    ).approved
    expanded = ConsentSnapshot(
        snapshot.selection_hash, snapshot.environment_hash,
        {**snapshot.contracts, "added": "b" * 64},
    )
    decision = check_consent(
        consent, expanded, tool="existing", implemented_contract="a" * 64,
    )
    assert not decision.approved
    assert decision.reason == "consent_changed"
