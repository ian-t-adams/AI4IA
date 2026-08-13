"""Pin which synthetic capabilities the approval gate covers, and which it does not.

`requires_invocation_approval` reads `spec.risk` off a `ToolSpec`, so a tool is
gateable only if something hands the runtime a spec for it. MCP tools get one from
the registry. The *synthetic* capabilities that `run_agent_turn` dispatches from
`extra_handlers` used to get one from nowhere at all -- they had an execution route
and no risk classification, so the gate could not see them. That was half of audit
finding P1-13, and the reason `browse_url` shipped as an unmitigated egress channel.

`agents/synthetic_governance.py` is the missing half. This file is its contract test,
in both directions:

* **Completeness.** Every capability declared anywhere in the API must appear in the
  table. It is not merely documented -- an unclassified capability is *refused* at
  runtime, so a missing entry is an outage, and this test is what turns that into a
  CI failure instead.
* **Posture.** The gated/ungated split is restated here independently of the source,
  with the reason for each, so a one-character edit to a `risk=` or an
  `injection_only_risk=` cannot quietly move a capability out from under the gate.
  This is the assertion that would have caught the original finding.

Nothing here imports a capability module: they pull in Azure SDKs and `Settings` at
import time, and several import *from* `agents`. Names are source-scanned instead,
using the `*_TOOL_NAME = "..."` convention every capability module already follows.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ai4ia_api.agents.approvals import ApprovalPolicy, requires_invocation_approval
from ai4ia_api.agents.synthetic_governance import SYNTHETIC_TOOL_SPECS
from ai4ia_api.agents.tool_exec import build_tools
from ai4ia_api.agents.tools import ToolRisk

API_SRC = Path(__file__).resolve().parents[1] / "src" / "ai4ia_api"

# Held under the default `always` policy, on every turn, tainted or not: the model
# chooses the destination or the code, so no server-side boundary constrains where
# the data goes. A human must see the specific call.
ALWAYS_GATED: dict[str, str] = {
    "browse_url": "Arbitrary URL fetch. The clearest exfiltration channel here: "
    "a poisoned document can name the destination.",
    "run_code": "Executes model-authored code in an Azure-managed sandbox reached "
    "directly rather than through the gateway, so the usual APIM-side controls "
    "do not apply to it either.",
    "analyze_attachment": "Uploads the attachment to the same direct Foundry "
    "sandbox and executes model-authored analysis over it; closure-bound file "
    "access does not make the external execution itself a safe read.",
}

# Held only on a turn that actually carried untrusted content. The destination is
# fixed by server configuration and the effect is confined to the caller's own
# data, so injected text choosing the payload is the whole of the risk -- and on a
# clean turn the only possible author of those arguments is the user.
GATED_WHEN_TAINTED: dict[str, str] = {
    "web_search": "Query text reaches the configured provider, so the query itself "
    "can carry exfiltrated content even though the host is fixed.",
    "news_search": "Query text reaches the provider; same shape as web_search.",
    "video_search": "Query text reaches the provider; same shape as web_search.",
    "image_search": "Query text reaches the provider; same shape as web_search.",
    "generate_image": "Bills a model deployment on a model-written prompt; the "
    "result is attached to the caller's own message, so nothing is exfiltrated.",
    "generate_video": "As generate_image, and materially more expensive.",
    "remember_memory": "Writes durable memory that is read back into later "
    "sessions -- a planted memory is a persistent foothold. The write is bound to "
    "the caller's own store and cannot be redirected, so a clean turn has nothing "
    "to gate. This is the only capability with live production usage.",
    "export_document": "Writes a new version of the caller's own document; the "
    "original stays immutable and the artifact is surfaced in the reply.",
}

# Never gated. Reads over the caller's own data: no egress, no durable write, user
# id closure-bound. They do import untrusted content into the turn, which is what
# the runtime's taint latch is for -- it gates the NEXT outbound call. Gating the
# read as well would prompt on the half of the chain that cannot leak.
UNGATED: dict[str, str] = {
    "recall_memory": "Reads durable memory into the turn.",
    "fetch_document": "Reads a stored library document into the turn.",
    "process_document": "Server-side document understanding over stored bytes.",
    "delegate_to_agent": "A router, not a capability: the sub-turn runs through "
    "the same governed runtime and is built with no extra_handlers, so it cannot "
    "re-enter this table. Bounded fan-out on the supervisor's own deployment.",
    "run_workflow": "Advertises only workflows whose resolved step tools are safe, "
    "filters nested capabilities to safe reads, and re-checks at execution time.",
}

EXPECTED_POSTURE: dict[str, str] = {
    **dict.fromkeys(ALWAYS_GATED, "always"),
    **dict.fromkeys(GATED_WHEN_TAINTED, "tainted"),
    **dict.fromkeys(UNGATED, "never"),
}

REASONS: dict[str, str] = {**ALWAYS_GATED, **GATED_WHEN_TAINTED, **UNGATED}

_TOOL_NAME_CONST = re.compile(r'^[A-Z_]*TOOL_NAME[A-Z_]*\s*=\s*"([a-z_]+)"', re.M)


def _synthetic_names_in_source() -> set[str]:
    """Tool names declared as `*_TOOL_NAME = "..."` constants across the API.

    A source scan rather than an import graph, because the capabilities are
    built by half a dozen factories with different signatures and importing them
    all would drag in Azure clients. The constant is the convention every
    capability module already follows.
    """
    found: set[str] = set()
    for path in API_SRC.rglob("*.py"):
        for match in _TOOL_NAME_CONST.finditer(path.read_text(encoding="utf-8", errors="replace")):
            found.add(match.group(1))
    return found


def _observed_posture(name: str) -> str:
    """What the gate actually does with this capability, derived from the spec."""
    spec = SYNTHETIC_TOOL_SPECS[name]
    gated_clean = requires_invocation_approval(
        spec, policy=ApprovalPolicy.always, untrusted_context=False
    )
    gated_tainted = requires_invocation_approval(
        spec, policy=ApprovalPolicy.always, untrusted_context=True
    )
    if gated_clean:
        return "always"
    return "tainted" if gated_tainted else "never"


def test_registry_governs_only_the_trivial_builtins() -> None:
    """The premise, unchanged: the registry itself still covers almost nothing.

    Every capability that reaches the network, spends money, executes code, or
    writes durable state is synthetic. That is why classifying them separately
    was necessary at all, and if this ever grows, the table below should shrink.
    """
    governed = {spec.name for spec in build_tools()[0].list()}
    assert governed == {"calculator", "get_current_time"}


def test_every_synthetic_capability_is_classified() -> None:
    """No capability may reach `extra_handlers` without a risk classification.

    An unclassified one is refused at runtime (`DenyReason.ungoverned`), so this
    failing means a feature is dead, not merely undocumented.
    """
    declared = _synthetic_names_in_source()
    # `mcp` is a namespace prefix, not a capability.
    declared.discard("mcp")
    missing = sorted(declared - set(SYNTHETIC_TOOL_SPECS))
    assert missing == [], (
        "These capabilities are dispatched from extra_handlers with no entry in "
        "agents/synthetic_governance.py, so the runtime will refuse them. Give "
        f"each one a ToolSpec and record why its posture is right: {missing}"
    )


def test_governance_table_has_no_stale_entries() -> None:
    """Keeps the table honest in the other direction."""
    declared = _synthetic_names_in_source()
    stale = sorted(set(SYNTHETIC_TOOL_SPECS) - declared)
    assert stale == [], f"no longer declared in the source, remove them: {stale}"


def test_expectations_cover_the_table_exactly() -> None:
    """The three lists above and the shipped table describe the same set."""
    assert sorted(EXPECTED_POSTURE) == sorted(SYNTHETIC_TOOL_SPECS)
    overlap = (set(ALWAYS_GATED) & set(GATED_WHEN_TAINTED)) | (
        set(UNGATED) & (set(ALWAYS_GATED) | set(GATED_WHEN_TAINTED))
    )
    assert overlap == set(), f"a capability cannot be in two postures: {sorted(overlap)}"


@pytest.mark.parametrize("name", sorted(EXPECTED_POSTURE))
def test_posture_matches_what_the_gate_actually_does(name: str) -> None:
    """The assertion that would have caught P1-13, restated per capability.

    Derived from `requires_invocation_approval` rather than from reading the
    spec's fields, so it tests the decision the runtime makes, not a restatement
    of the data it makes it from.
    """
    assert _observed_posture(name) == EXPECTED_POSTURE[name]


def test_the_two_egress_primitives_are_never_relaxed() -> None:
    """`injection_only_risk` is a claim about a *fixed* destination.

    Setting it on a capability whose destination or program the model chooses
    would be a lie, and would silently downgrade the exact control the audit
    asked for. Pinned separately from the posture table because it is the one
    edit that looks harmless in review.
    """
    for name in ALWAYS_GATED:
        spec = SYNTHETIC_TOOL_SPECS[name]
        assert spec.risk is not ToolRisk.safe, name
        assert spec.injection_only_risk is False, name


def test_off_is_still_a_real_opt_out_for_synthetic_capabilities() -> None:
    """`off` means off -- `injection_only_risk` only ever softens `always`."""
    for name, spec in SYNTHETIC_TOOL_SPECS.items():
        assert (
            requires_invocation_approval(
                spec, policy=ApprovalPolicy.off, untrusted_context=True
            )
            is False
        ), name


def test_every_capability_has_a_reason() -> None:
    blank = sorted(k for k, v in REASONS.items() if len(v.strip()) < 20)
    assert blank == [], f"entries need a real justification, not a placeholder: {blank}"


def test_every_spec_carries_a_human_readable_purpose() -> None:
    """The description is rendered on the approval card, so it must be prose.

    A card that says `browse_url` and nothing else asks the user to approve a
    string, which is not a decision they can make.
    """
    for name, spec in SYNTHETIC_TOOL_SPECS.items():
        assert len(spec.description.strip()) >= 30, name
        assert name not in spec.description, f"{name}: describe the effect, not the symbol"
