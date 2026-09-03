"""Risk classification for the **synthetic** capabilities (audit finding P1-13).

``agents/tools.py`` governs anything that reaches an agent through the tool
registry. Synthetic capabilities never do: they are built per turn, closure-bound
to the authenticated user, and handed to
:func:`~ai4ia_api.agents.runtime.run_agent_turn` as ``extra_handlers``. That gave
them an execution route with no risk classification, so the per-invocation
approval gate (:mod:`ai4ia_api.agents.approvals`) could not see them at all —
including ``browse_url``, which is the exfiltration primitive the audit named.

This module is the missing half. Every synthetic capability gets a real
:class:`~ai4ia_api.agents.tools.ToolSpec`, so **one** definition of risk serves
both dispatch paths and the operator's existing ``ApprovalPolicy`` decides what
happens. There is no parallel "gated names" list and no second policy engine.

**Why a table of string literals rather than imported constants.** Every
capability module (`websearch.capability`, `library.compute_capability`,
`images.capability`, ...) pulls in Azure SDKs, ``Settings``, and service classes
at import time. ``agents/runtime.py`` is imported by everything and must not drag
that in, and several of those modules import *from* ``agents``. The names are
therefore restated here and their completeness is proved by a source scan in
``tests/test_ungated_capabilities.py``: a capability declared anywhere in the API
without an entry here fails CI, and at runtime an unclassified capability is
refused (``DenyReason.ungoverned``) rather than run.

**How the three postures were chosen.** The question for each capability is *what
can an attacker who controls a document in the user's library actually do with
it*, not *how expensive does it sound*.

``always`` — the model chooses the destination or the code
    The attacker names the endpoint or authors the program. No fixed server-side
    boundary constrains where the data goes or what runs. A human must see the
    specific call.

``injection_only_risk`` — the destination is fixed, the payload is not
    The server decides where the call goes and whose data it touches; the only
    thing injected text can steer is *what* is sent or written. That risk exists
    exactly when the turn carried untrusted content, so these gate at ``tainted``
    strength even under ``always``. On a clean turn the arguments can only have
    come from the user, and prompting there is friction with no security value.

``safe`` — reads the caller's own data, no egress, no durable write
    Nothing leaves and nothing persists. These *do* import untrusted content into
    the turn, but the control for that is the taint latch in ``run_agent_turn``
    (any tool result sets ``untrusted_context``), which then gates the next
    outbound call. Gating the read as well would prompt on the safe half of the
    chain and change nothing about the dangerous half.

Measured before choosing: production ``AppEvents`` for the 30 days to 2026-08-06
recorded 10 tool invocations, all ``remember_memory``, and zero of every
capability moved to ``always`` here. The classification is argued from the threat
model, not from that number — but it is why ``remember_memory``'s posture is the
one worth being careful about, and why the rest cost nothing today.
"""
from __future__ import annotations

from .tools import ToolRisk, ToolSpec

# --- Egress: the model chooses the destination ---------------------------------

_BROWSE_URL = ToolSpec(
    name="browse_url",
    # Shown verbatim on the approval card, so it is written for the human being
    # asked, not for the model.
    description="Fetch a web page the model chose and read its contents back into the conversation.",
    risk=ToolRisk.external,
    # NOT injection_only_risk. The destination is the `url` argument: a poisoned
    # document names the host, and the query string carries the payload. This is
    # the audit's named channel and the reason this module exists.
)

# --- Egress: the destination is fixed, the query text is not --------------------
#
# Four verticals of one Web IQ call. The host is server configuration the model
# cannot influence, and a search provider is a poor exfiltration endpoint (the
# attacker would have to read the provider's logs). What remains is real though:
# injected text choosing the query, which both leaks the query and steers which
# untrusted pages come back. That is precisely an injection-only risk, so a user
# who types "search the web for X" is not interrupted, while the same call made
# on a turn holding a poisoned document is held.

_SEARCH_DESCRIPTIONS = {
    "web_search": "Send a search query to the configured web search provider.",
    "news_search": "Send a news search query to the configured web search provider.",
    "video_search": "Send a video search query to the configured web search provider.",
    "image_search": "Send an image search query to the configured web search provider.",
}

# --- Execution ------------------------------------------------------------------

_RUN_CODE = ToolSpec(
    name="run_code",
    description=(
        "Run model-authored code over one of your documents in a sandboxed "
        "container, and read the result back into the conversation."
    ),
    risk=ToolRisk.external,
    # NOT injection_only_risk, for two reasons that are independent of each other.
    # The model authors the program, so there is no fixed effect to bound; and
    # this stateful Azure-managed sandbox cannot traverse SimpleL7Proxy. It uses
    # a dedicated APIM API that fixes the model/store/tool contract, but APIM
    # cannot prove that arbitrary model-authored code is safe. That earns a human.
)

_ANALYZE_ATTACHMENT = ToolSpec(
    name="analyze_attachment",
    description=(
        "Upload an attachment to the APIM-fronted Foundry Code Interpreter sandbox, "
        "run model-authored analysis over it, and read the result back."
    ),
    risk=ToolRisk.external,
    # This is the same stateful sandbox primitive as run_code. The closure-bound
    # user/file access prevents cross-user reads, but it does not bound what code
    # the model asks the remote sandbox to run or remove the provider side effect.
    # Treating it as a safe read let a poisoned attachment trigger the very
    # primitive that run_code always asks a human to approve.
)

# --- Generation: fixed destination, model-chosen prompt, real spend -------------

_GENERATE_IMAGE = ToolSpec(
    name="generate_image",
    description="Generate an image from a model-written prompt and attach it to the reply.",
    risk=ToolRisk.external,
    injection_only_risk=True,
    # The deployment is catalog-resolved and the result is attached to the
    # caller's own message, so nothing is exfiltrated. The exposure is spend and
    # content attributed to the user, and only injected text can cause either
    # without the user asking. "Draw me a cat" should not raise a prompt.
)

_GENERATE_VIDEO = ToolSpec(
    name="generate_video",
    description="Generate a video from a model-written prompt and attach it to the reply.",
    risk=ToolRisk.external,
    injection_only_risk=True,
    # As generate_image, and materially more expensive per call -- which is an
    # argument for a spend control, not for an approval prompt. A prompt the user
    # clicks through on every legitimate request teaches them to click through.
)

# --- Durable per-user writes ----------------------------------------------------

_REMEMBER_MEMORY = ToolSpec(
    name="remember_memory",
    description="Save a fact to your durable memory, where it will be recalled in future sessions.",
    risk=ToolRisk.destructive,
    injection_only_risk=True,
    # The posture worth arguing about: this is the ONLY capability with live
    # production usage (10 invocations in the 30 days to 2026-08-06; every other
    # capability here: zero), so it is the only one a user would feel.
    #
    # `destructive` because it writes state that outlives the turn and is read
    # back into later ones -- a planted memory is a persistent foothold, which is
    # strictly worse than a one-off exfiltration.
    #
    # `injection_only_risk` because the write goes to the caller's own store,
    # closure-bound to their user id, and cannot be redirected by any argument.
    # The turn either carried untrusted content -- in which case the memory may
    # have been planted, and it is held -- or it did not, in which case the user
    # is the only possible author of the thing being remembered and asking them
    # to approve their own sentence is pure friction. Under `always` this is the
    # difference between a control the user reads and one they learn to dismiss.
)

_EXPORT_DOCUMENT = ToolSpec(
    name="export_document",
    description="Write model-adjusted content as a new version of one of your documents.",
    risk=ToolRisk.destructive,
    injection_only_risk=True,
    # A durable write, so `destructive`; but owner-only, into the caller's own
    # library, as a NEW version that leaves the original immutable, and the
    # resulting artifact is surfaced in the reply. A poisoned document could
    # cause a poisoned new version, which is why it is gated on taint at all.
)

# --- Reads over the caller's own data -------------------------------------------
#
# `safe` is load-bearing here, not a shrug: no egress, no durable write, and the
# user id is closure-bound so no argument can reach another user's data. Each one
# does pull untrusted content into the turn -- which is exactly what the taint
# latch in `run_agent_turn` exists for. Gating the read would prompt on the half
# of the chain that cannot leak, while the half that can is already covered.

_READ_ONLY_DESCRIPTIONS = {
    "recall_memory": "Read your durable memory into this conversation.",
    "fetch_document": "Read one of your library documents into this conversation.",
    "process_document": "Extract structure from one of your stored documents.",
}

# --- Orchestration ---------------------------------------------------------------

_DELEGATE_TO_AGENT = ToolSpec(
    name="delegate_to_agent",
    description="Ask one of your linked agents to answer a self-contained sub-task.",
    risk=ToolRisk.safe,
    # A router, not a new capability. The sub-turn runs through this same
    # `run_agent_turn` with its own governance, and is constructed with neither
    # `extra_tools` nor `extra_handlers` (see agents/orchestration.py), so it
    # cannot re-enter this table -- it reaches only registry tools, which are
    # gated by the registry path. Delegation is bounded to
    # MAX_DELEGATIONS_PER_TURN sub-turns on the supervisor's own deployment, so
    # the fan-out is a metered cost rather than an unbounded one, and the target
    # is constrained to the agent's own configured `links`.
)

_RUN_WORKFLOW = ToolSpec(
    name="run_workflow",
    description="Run one of the user's saved safe workflows over a bounded input.",
    risk=ToolRisk.safe,
    # The capability advertises only workflows whose resolved step tools are safe,
    # filters nested synthetic capabilities to safe reads, and restores the normal
    # approval policy as a second guard. It is chat-only to prevent recursion.
)


def _search_spec(name: str, description: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        risk=ToolRisk.external,
        injection_only_risk=True,
    )


def _read_spec(name: str, description: str) -> ToolSpec:
    return ToolSpec(name=name, description=description, risk=ToolRisk.safe)


#: Every synthetic capability the API can inject, keyed by the name the model
#: calls. :func:`~ai4ia_api.agents.runtime.run_agent_turn` resolves a dispatchable
#: handler's spec here and runs it through the same gate as a registry tool.
SYNTHETIC_TOOL_SPECS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        _BROWSE_URL,
        *(_search_spec(n, d) for n, d in _SEARCH_DESCRIPTIONS.items()),
        _RUN_CODE,
        _ANALYZE_ATTACHMENT,
        _GENERATE_IMAGE,
        _GENERATE_VIDEO,
        _REMEMBER_MEMORY,
        _EXPORT_DOCUMENT,
        *(_read_spec(n, d) for n, d in _READ_ONLY_DESCRIPTIONS.items()),
        _DELEGATE_TO_AGENT,
        _RUN_WORKFLOW,
    )
}


def synthetic_spec(name: str) -> ToolSpec | None:
    """The governing spec for a synthetic capability, or ``None`` if unclassified.

    ``None`` is a denial, not a pass: see the ``ungoverned`` branch in
    :func:`~ai4ia_api.agents.runtime.run_agent_turn`.
    """
    return SYNTHETIC_TOOL_SPECS.get(name)
