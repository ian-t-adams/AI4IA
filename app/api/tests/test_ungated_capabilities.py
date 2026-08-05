"""Pin which capabilities sit outside the per-invocation approval gate.

`requires_invocation_approval` reads `spec.risk` off a `ToolSpec`, so a tool is
gateable only if it goes through the registry. MCP tools do. The *synthetic*
capabilities that `run_agent_turn` dispatches from `extra_handlers` before the
registry path do not -- they have no `ToolSpec` to read a risk from, so the gate
cannot see them at all.

The audit (P1-13) named `browse_url` as the exception. Measuring showed the
exception is the rule: the registry governs exactly two built-ins, `calculator`
and `get_current_time`, both `safe`. Every capability that actually reaches the
network, spends money, executes code, or writes durable state is synthetic and
ungated. That is a much larger statement than "`browse_url` is not covered", and
it is the kind of scope claim that decays quietly, so it is asserted here.

This file deliberately does **not** change behaviour. Gating these would prompt
the user on every web search or document fetch, which is a product decision, not
a bug fix. What it prevents is the inventory growing by accident: adding a
sixteenth synthetic capability now fails this test until someone records why it
is safe to leave ungated, or gives it a `ToolSpec`.
"""

from __future__ import annotations

import re
from pathlib import Path

from ai4ia_api.agents.tool_exec import build_tools

API_SRC = Path(__file__).resolve().parents[1] / "src" / "ai4ia_api"

# Every synthetic capability the chat router can inject, with why it is not
# behind the per-invocation approval gate today. Adding an entry is a deliberate
# act; the assertions below fail until the inventory matches the source.
UNGATED_SYNTHETIC_CAPABILITIES: dict[str, str] = {
    # --- reaches the public internet -------------------------------------
    "browse_url": "Arbitrary URL fetch. The clearest exfiltration channel here: "
    "a poisoned document can name the destination.",
    "web_search": "Query text reaches the search provider, so the query itself "
    "can carry exfiltrated content even though the host is fixed.",
    "news_search": "Query text reaches the provider; same exfiltration shape as "
    "web_search, different vertical.",
    "video_search": "Query text reaches the provider; same exfiltration shape as "
    "web_search, different vertical.",
    "image_search": "Query text reaches the provider; same exfiltration shape as "
    "web_search, different vertical.",
    # --- executes or generates -------------------------------------------
    "run_code": "Executes model-authored code in the Foundry sandbox.",
    "generate_image": "Bills a model deployment; prompt is model-chosen.",
    "generate_video": "Bills a model deployment; prompt is model-chosen.",
    # --- reads or writes durable per-user state --------------------------
    "remember_memory": "Writes durable memory the user did not type.",
    "recall_memory": "Reads durable memory into the turn.",
    "process_document": "Server-side document understanding over stored bytes.",
    "analyze_attachment": "As process_document, for an inline attachment.",
    "fetch_document": "Reads a stored library document into the turn.",
    "export_document": "Produces a downloadable artifact.",
    # --- orchestration ----------------------------------------------------
    "delegate_to_agent": "Runs another agent turn; that turn's own tools are "
    "governed, so this is a router rather than a new egress.",
}

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


def test_registry_governs_only_the_trivial_builtins() -> None:
    """The premise, and the surprising part.

    If this ever grows, a real capability has become gateable and the inventory
    below should shrink to match.
    """
    governed = {spec.name for spec in build_tools()[0].list()}
    assert governed == {"calculator", "get_current_time"}


def test_ungated_inventory_matches_the_source() -> None:
    """No synthetic capability may appear without being classified."""
    declared = _synthetic_names_in_source()
    # `mcp` is a namespace prefix, not a capability.
    declared.discard("mcp")
    missing = sorted(declared - set(UNGATED_SYNTHETIC_CAPABILITIES))
    assert missing == [], (
        "New synthetic capabilities are dispatched before the registry and are "
        "therefore outside the per-invocation approval gate. Record why each is "
        "safe to leave ungated, or give it a ToolSpec so the gate can see it: "
        f"{missing}"
    )


def test_inventory_has_no_stale_entries() -> None:
    """Keeps the list honest in the other direction."""
    declared = _synthetic_names_in_source()
    stale = sorted(set(UNGATED_SYNTHETIC_CAPABILITIES) - declared)
    assert stale == [], f"no longer declared in the source, remove them: {stale}"


def test_every_ungated_capability_has_a_reason() -> None:
    blank = sorted(k for k, v in UNGATED_SYNTHETIC_CAPABILITIES.items() if len(v.strip()) < 20)
    assert blank == [], f"entries need a real justification, not a placeholder: {blank}"
