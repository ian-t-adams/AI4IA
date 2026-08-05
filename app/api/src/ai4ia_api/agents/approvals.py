"""Per-**invocation** human approval for governed tool calls.

``agents/tools.py`` governs a tool *as a thing*: a :class:`~ai4ia_api.agents.tools.ToolSpec`
declares whether that tool needs approval, and ``ToolContext.approvals`` carries a
**standing** set of tool names the caller has blessed for the whole turn. That model
is fine for "may this agent use this tool at all", and it is exactly wrong for the
threat this module exists to close.

**The threat.** Retrieved documents, recalled memory, library excerpts, web results
and previous MCP responses are all promoted into a turn's context. They are fenced
with a per-message nonce, which defeats naive delimiter escapes, but a fence is not
an information-flow boundary: text inside it can still influence what the model
*decides to do*. If an external tool is standing-approved (a ``trusted`` MCP server,
or a per-tool ``requireApproval: never`` override), a hostile document can therefore
choose the **arguments** of a real outbound call — shipping other context to a
destination the attacker picked, with no human ever seeing it.

**The control.** Approval becomes a property of *one call with one exact set of
arguments*, not of the tool. Concretely:

1. The runtime hashes the arguments the model actually produced
   (:func:`arguments_digest` over canonical JSON) and refuses to execute a gated
   tool unless ``ToolContext.invocation_approvals`` contains that exact
   ``(tool, digest)`` pair (:func:`approval_key`).
2. When it refuses, it mints an :class:`ApprovalDraft` describing the call — tool,
   destination host, purpose, risk, argument digest, and a **redacted** argument
   preview (:func:`build_preview`, which reuses ``tools.redact_obj``; there is
   deliberately no second redactor in this codebase).
3. The chat router turns each draft into a durable :class:`PendingToolApproval`
   stored on the assistant message, plus a one-time ``grant`` string returned to
   the browser exactly once over SSE. Only the grant's SHA-256 is persisted, so
   reading the stored record does not let anyone approve anything.
4. The user approves. The next turn presents ``{requestId, grant}``. The server
   re-derives the record **from its own storage** (never from the client) and
   re-checks every binding before turning it into an invocation approval.

**What binds an approval, and where each binding is enforced:**

===================  ==========================================================
binding              enforced by
===================  ==========================================================
user id              the record lives inside that user's session; the session
                     repository ownership-checks every read.
session id           the record is looked up only among *this* session's
                     messages, so a grant minted elsewhere is simply not found.
tool identity        :attr:`PendingToolApproval.tool` -> the invocation key.
argument digest      :attr:`PendingToolApproval.argumentsDigest` -> the
                     invocation key, re-derived at execution time from the
                     arguments the model actually emitted. Change one character
                     of one argument and the key no longer matches.
expiry               :attr:`PendingToolApproval.expiresAt`, checked at consume
                     time (:func:`consume_grant`).
single use           two independent mechanisms, closing different holes.
                     ``PendingToolApproval.consumed`` is flipped through the
                     repository's **conditional** (ETag) write, so two
                     concurrent requests presenting the same grant cannot both
                     redeem it; and ``run_agent_turn`` removes the redeemed key
                     from its per-turn set on first dispatch, so one approval
                     cannot cover a model that emits the same call repeatedly
                     within one turn. The second is not a refinement of the
                     first: redemption happens once per turn, while the model's
                     tool-call list is exactly what injected context controls.
possession           ``sha256(grant) == grantHash``, ``hmac.compare_digest``.
===================  ==========================================================

**A client cannot mint one.** The grant is 256 bits of ``secrets.token_urlsafe``
material generated server-side; the client only ever holds an opaque string whose
digest the server already committed to. Guessing a ``requestId`` is useless without
the matching grant, and holding a grant is useless once the record is consumed,
expired, or asked to authorize different arguments. FastAPI is the trust boundary
here in the same way ``ChatParams`` is in ``routers/chat.py``.

**Scope, stated plainly.** :func:`requires_invocation_approval` gates tools that
carry a :class:`~ai4ia_api.agents.tools.ToolSpec` through the registry — which is
every MCP tool on both planes (they are all ``external`` risk). It does **not** gate
the synthetic capabilities that the runtime dispatches from ``extra_handlers``
before the registry path (web search, ``browse_url``, code execution, document
processing): those have no ``ToolSpec`` to read a risk off, so they are outside this
seam today. ``browse_url`` in particular remains an unmitigated egress channel. The
seam for closing that gap is marked in ``runtime.run_agent_turn``.

Likewise, ``untrusted_context`` is a **turn-level** taint bit, not per-argument
provenance: it says "this turn had untrusted content in it", not "this specific
string came from a document". Real dataflow tracking would attach provenance to
each retrieved span and follow it into argument construction; that is a larger
change and is deliberately not claimed here.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .tools import (
    ToolRisk,
    ToolSpec,
    is_fully_masked,
    is_safe_tool_name,
    redact,
    redact_obj,
)

# How long a minted approval stays redeemable. Short on purpose: an approval is a
# capability to make one specific outbound call, and the user is looking at the
# prompt when it is minted. Long enough to read a preview and decide; far too
# short to be a durable standing grant.
APPROVAL_TTL_SECONDS = 600

# Per-turn ceiling on how many distinct approvals one turn may request. Without
# it, a model driven by injected text could paper the UI with prompts until the
# user click-throughs one. Excess requests are dropped (the call is still denied).
MAX_APPROVAL_REQUESTS_PER_TURN = 4

# Bounds on what reaches the approval card. These are user-facing strings built
# from remote-server-supplied descriptions and model-supplied arguments, so they
# are capped and redacted before they are persisted or streamed.
#
# ``_MAX_PREVIEW_ENTRIES`` is a *last-resort* cap, not a display budget. It used
# to be 12 and silently dropped everything past it in sort order, which handed
# the attacker the card: the argument set and the key names are both
# model-controlled (``validate_args`` deliberately tolerates properties outside
# the declared schema and the MCP handler forwards them verbatim), so padding
# with filler keys that sort early pushed an exfiltration's ``to`` off the card
# while it still went out on the wire. Keys are now shown to a much higher cap,
# per-value length shrinks as the key count grows so the payload stays bounded,
# and anything still dropped is counted and surfaced rather than vanishing.
_MAX_PREVIEW_ENTRIES = 40
_MAX_PREVIEW_VALUE_CHARS = 200
# Total character budget across all preview values. Per-value length is this
# divided by the number of keys shown (floored), so a 40-key call still fits in
# a bounded payload without any key disappearing.
_PREVIEW_VALUE_BUDGET = 1600
_MIN_PREVIEW_VALUE_CHARS = 24
_MAX_PURPOSE_CHARS = 240
_MAX_LABEL_CHARS = 120

# 256 bits. The grant is a bearer capability for exactly one call.
_GRANT_BYTES = 32

# Sentinel used when a tool name is not safe to surface verbatim, mirroring the
# runtime/activity boundary rule (see ``tools.is_safe_tool_name``).
_UNKNOWN_TOOL = "unknown_tool"


class ApprovalPolicy(str, Enum):
    """When a governed call needs a fresh, per-invocation human approval.

    ``off`` restores the pre-existing behavior exactly: standing approvals
    (``trusted`` server / ``requireApproval: never``) are sufficient and no live
    prompt is ever raised. It exists so an operator can opt out deliberately and
    visibly, not because it is a reasonable default.

    ``tainted`` raises a prompt only when the turn actually carried untrusted
    content (documents, recalled memory, library excerpts, or an earlier tool
    result in the same turn). It preserves the ergonomics of a trusted server for
    turns with no injection surface.

    ``always`` (the default) raises a prompt for every external/destructive call.

    **Approval identity vs. endpoint identity.** An approval is keyed on the
    runtime tool alias, and :func:`~ai4ia_api.agents.mcp_servers.tool_alias`
    hashes ``(plane, server_name, raw_tool_name)`` — deliberately *not* the
    endpoint URL, because the alias must stay stable across a turn and the URL is
    not part of the model-facing identity. Consequence: if the owner re-points a
    registered server name at a different URL inside an approval's TTL, an
    approval minted against the old endpoint still key-matches. That is outside
    this module's threat model (it takes an authenticated action *by the
    approver*, not injected text, and the destination host shown on the card came
    from the spec at mint time), but it is the kind of thing that is much cheaper
    to know than to rediscover. Shortening the TTL or folding the host into the
    key would both close it if the threat model ever widens.
    """

    off = "off"
    tainted = "tainted"
    always = "always"


# --- Argument identity ---------------------------------------------------------


def canonical_arguments(arguments: Mapping[str, Any] | None) -> str:
    """Canonical JSON for an argument object: sorted keys, no incidental spacing.

    Two argument objects that differ only in key order or formatting must produce
    the same digest (otherwise an approval the user granted would spuriously fail
    to match), and any difference in an actual value must produce a different one.
    ``default=str`` keeps a non-JSON-native value (a stray ``datetime`` from a
    handler-built dict) from raising instead of hashing.
    """
    return json.dumps(
        arguments or {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def arguments_digest(arguments: Mapping[str, Any] | None) -> str:
    """SHA-256 over :func:`canonical_arguments` — the call's argument identity."""
    return hashlib.sha256(canonical_arguments(arguments).encode("utf-8")).hexdigest()


def approval_key(tool: str, digest: str) -> str:
    """The exact ``(tool, arguments)`` pair an approval authorizes.

    ``\\x00`` cannot appear in either component, so the join is unambiguous: no
    ``tool``/``digest`` pair can be spoofed by another pair's concatenation.
    """
    return f"{tool}\x00{digest}"


# --- Policy --------------------------------------------------------------------


def requires_invocation_approval(
    spec: ToolSpec,
    *,
    policy: ApprovalPolicy,
    untrusted_context: bool,
) -> bool:
    """Whether *this* call needs a fresh approval, independent of standing ones.

    Deliberately ignores ``spec.requires_approval`` / ``spec.needs_approval``:
    those are the *standing* posture (and are what a ``trusted`` server switches
    off), which is precisely the property this gate must not inherit. A trusted
    external tool is still an outbound call whose arguments a hostile document
    could have chosen.
    """
    if policy is ApprovalPolicy.off:
        return False
    reaches_out = spec.risk in (ToolRisk.external, ToolRisk.destructive)
    if not reaches_out:
        return False
    if policy is ApprovalPolicy.always:
        return True
    return untrusted_context


# --- Redacted preview ----------------------------------------------------------


def _preview_value(value: Any, limit: int) -> tuple[str, bool]:
    """Single-line display form of one argument value, and whether it was cut."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):  # pragma: no cover - default=str covers these
            text = str(value)
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        return collapsed[:limit] + "…", True
    return collapsed, False


@dataclass(frozen=True)
class ArgumentPreview:
    """What a human is shown about one call's arguments, and what they are not.

    The digest binds the **whole** argument object; this preview is the only
    part a person actually reads. Those two must not be allowed to disagree
    silently, because the control's entire value is the human's judgement about
    where data is going. So the preview reports its own incompleteness:

    * ``shown`` — key -> display value, for every key that fits.
    * ``masked`` — keys whose *value* was replaced by the shared redactor. The
      card must render these differently from a value it is showing verbatim:
      ``***REDACTED***`` means "hidden from you, but sent in full", which is a
      materially different claim from "this is what will be sent".
    * ``elided`` — keys whose value was length-capped (the value ends in ``…``).
    * ``omitted`` — count of keys not shown **at all**. Non-zero means the card
      is not the whole call and must say so.
    """

    shown: dict[str, str] = field(default_factory=dict)
    masked: frozenset[str] = field(default_factory=frozenset)
    elided: frozenset[str] = field(default_factory=frozenset)
    omitted: int = 0

    @property
    def truncated(self) -> bool:
        """Whether any key was dropped entirely — i.e. the card is incomplete."""
        return self.omitted > 0


def build_preview(arguments: Mapping[str, Any] | None) -> ArgumentPreview:
    """A bounded, credential-redacted, single-line view of the call's arguments.

    This is what a human is asked to judge, so it must show enough to spot an
    exfiltration attempt (the destination, the payload's shape) without becoming
    a new place secrets land. Redaction is ``tools.redact_obj`` — the same
    redactor the runtime's trace and logs use — applied *before* stringification,
    so a credential-named key is masked wholesale rather than merely truncated.

    Keys are **never silently dropped**: the per-value budget shrinks as the key
    count grows, and anything beyond the hard cap is reported in ``omitted``
    rather than disappearing. See ``_MAX_PREVIEW_ENTRIES``.
    """
    source = dict(arguments or {})
    redacted = redact_obj(source)
    if not isinstance(redacted, Mapping):  # pragma: no cover - redact_obj preserves dicts
        return ArgumentPreview()

    keys = [key for key in sorted(redacted, key=str) if " ".join(str(key).split())]
    dropped = len(redacted) - len(keys)
    visible, overflow = keys[:_MAX_PREVIEW_ENTRIES], keys[_MAX_PREVIEW_ENTRIES:]
    per_value = _MAX_PREVIEW_VALUE_CHARS
    if visible:
        per_value = max(
            _MIN_PREVIEW_VALUE_CHARS,
            min(_MAX_PREVIEW_VALUE_CHARS, _PREVIEW_VALUE_BUDGET // len(visible)),
        )

    shown: dict[str, str] = {}
    masked: set[str] = set()
    elided: set[str] = set()
    for key in visible:
        label = " ".join(str(key).split())[:_MAX_LABEL_CHARS]
        value, was_cut = _preview_value(redacted[key], per_value)
        shown[label] = value
        # ``redact_obj`` masks a credential-named key's value wholesale; compare
        # against the redacted value rather than re-deriving which names count as
        # credentials, so there is exactly one definition of that in the codebase.
        if is_fully_masked(redacted[key]):
            masked.add(label)
        elif was_cut:
            elided.add(label)
    return ArgumentPreview(
        shown=shown,
        masked=frozenset(masked),
        elided=frozenset(elided),
        omitted=len(overflow) + dropped,
    )


def _bounded_purpose(text: str) -> str:
    collapsed = " ".join(redact(text or "").split())
    if len(collapsed) > _MAX_PURPOSE_CHARS:
        return collapsed[:_MAX_PURPOSE_CHARS] + "…"
    return collapsed


def _safe_label(*candidates: str | None) -> str:
    """First candidate that is safe to surface verbatim, else the sentinel.

    Tool identities can originate from a remote MCP server this codebase never
    reviewed, so an approval card — a persisted, streamed, user-facing surface —
    holds them to the same bounded charset as logs and activity entries.
    """
    for candidate in candidates:
        if candidate and is_safe_tool_name(candidate):
            return candidate
    return _UNKNOWN_TOOL


# --- The draft the runtime produces --------------------------------------------


@dataclass(frozen=True)
class ApprovalDraft:
    """One denied call, described well enough for a human to judge it.

    Produced by the runtime (which knows the call) and consumed by the chat
    router (which knows how to persist and stream it). It carries no secret and
    no grant — minting is the router's job, so the runtime never has to reason
    about durability or transport.
    """

    tool: str
    label: str
    host: str | None
    purpose: str
    risk: str
    arguments_digest: str
    preview: ArgumentPreview

    @property
    def key(self) -> str:
        return approval_key(self.tool, self.arguments_digest)


def draft_for_call(
    spec: ToolSpec,
    *,
    tool: str,
    label: str | None,
    arguments: Mapping[str, Any] | None,
    digest: str | None = None,
) -> ApprovalDraft:
    """Build a bounded, redacted draft for one denied invocation."""
    hosts = sorted(spec.egress_allowlist)
    return ApprovalDraft(
        tool=tool,
        label=_safe_label(label, tool),
        host=hosts[0] if len(hosts) == 1 else None,
        purpose=_bounded_purpose(spec.description),
        risk=spec.risk.value,
        arguments_digest=digest if digest is not None else arguments_digest(arguments),
        preview=build_preview(arguments),
    )


class ApprovalSink:
    """Bounded, de-duplicating collector for one turn's approval requests.

    Threaded through :class:`~ai4ia_api.agents.tool_exec.ToolContext` so the
    runtime can report "this call needs a human" without knowing anything about
    Cosmos, SSE, or message rows. De-duplication is by ``(tool, digest)``: a model
    that retries the identical call in the same turn produces one prompt.
    """

    def __init__(self, *, limit: int = MAX_APPROVAL_REQUESTS_PER_TURN) -> None:
        self._limit = max(0, limit)
        self._drafts: list[ApprovalDraft] = []
        self._seen: set[str] = set()
        self.dropped = 0

    def request(self, draft: ApprovalDraft) -> bool:
        """Record ``draft``; return whether it was newly recorded."""
        if draft.key in self._seen:
            return False
        if len(self._drafts) >= self._limit:
            self.dropped += 1
            return False
        self._seen.add(draft.key)
        self._drafts.append(draft)
        return True

    def drafts(self) -> list[ApprovalDraft]:
        return list(self._drafts)

    def __len__(self) -> int:
        return len(self._drafts)


# --- The durable record + its one-time grant -----------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def grant_hash(grant: str) -> str:
    return hashlib.sha256(grant.encode("utf-8")).hexdigest()


class PendingToolApproval(BaseModel):
    """A server-minted approval request, persisted on the assistant message.

    Additive and optional exactly like ``Message.steps``/``safety``: existing
    Cosmos documents lack the field and deserialize to ``None``, so there is no
    migration and no derived store to rebuild.

    The grant itself is **not** here — only ``grantHash``. The record is returned
    to the browser by ``GET /api/sessions/{id}/messages``, so storing the grant
    would make possession meaningless: anyone able to read the conversation could
    approve its outbound calls. The grant travels once, over the SSE stream that
    raised the prompt.
    """

    id: str
    # Runtime dispatch identity (the provider-safe alias for an MCP tool). This is
    # what the invocation key is built from, so it must match what the next turn's
    # registry will call the tool.
    tool: str
    # Durable, user-facing name (e.g. ``mcp:weather/forecast``) when it is safe to
    # surface; falls back to the alias, then to a fixed sentinel.
    label: str
    host: str | None = None
    purpose: str = ""
    risk: str = ToolRisk.external.value
    argumentsDigest: str
    argumentsPreview: dict[str, str] = Field(default_factory=dict)
    # Keys whose value the shared redactor masked wholesale. The card must show
    # these as "hidden from you, but sent in full" rather than as the value.
    argumentsMasked: list[str] = Field(default_factory=list)
    # Keys whose value was length-capped for display (value ends in "…").
    argumentsElided: list[str] = Field(default_factory=list)
    # Count of arguments NOT shown at all. Non-zero means the card is not the
    # whole call; the UI is required to say so, because a silently-shortened
    # preview is exactly how a caller-chosen argument set hides a destination.
    argumentsOmitted: int = 0
    grantHash: str
    consumed: bool = False
    expiresAt: datetime
    createdAt: datetime = Field(default_factory=_now)

    def is_expired(self, *, now: datetime | None = None) -> bool:
        moment = now or _now()
        expires = self.expiresAt
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return moment >= expires

    @property
    def key(self) -> str:
        return approval_key(self.tool, self.argumentsDigest)


def mint_pending_approval(
    draft: ApprovalDraft,
    *,
    now: datetime | None = None,
    ttl_seconds: int = APPROVAL_TTL_SECONDS,
) -> tuple[PendingToolApproval, str]:
    """Mint the durable record plus the one-time grant handed to the browser.

    Returns ``(record, grant)``. The caller persists ``record`` and streams
    ``grant`` exactly once; nothing else may ever see the grant.
    """
    moment = now or _now()
    grant = secrets.token_urlsafe(_GRANT_BYTES)
    record = PendingToolApproval(
        id=secrets.token_hex(16),
        tool=draft.tool,
        label=draft.label,
        host=draft.host,
        purpose=draft.purpose,
        risk=draft.risk,
        argumentsDigest=draft.arguments_digest,
        argumentsPreview=dict(draft.preview.shown),
        argumentsMasked=sorted(draft.preview.masked),
        argumentsElided=sorted(draft.preview.elided),
        argumentsOmitted=draft.preview.omitted,
        grantHash=grant_hash(grant),
        expiresAt=moment + timedelta(seconds=max(1, ttl_seconds)),
        createdAt=moment,
    )
    return record, grant


class ApprovalDenied(str, Enum):
    """Why a presented grant did not authorize anything. All fail closed."""

    unknown_request = "unknown_request"
    bad_grant = "bad_grant"
    expired = "expired"
    already_used = "already_used"


@dataclass(frozen=True)
class ApprovalOutcome:
    """Result of redeeming one presented ``{requestId, grant}`` pair."""

    record: PendingToolApproval | None = None
    reason: ApprovalDenied | None = None

    @property
    def granted(self) -> bool:
        return self.record is not None


def consume_grant(
    record: PendingToolApproval | None,
    grant: str | None,
    *,
    now: datetime | None = None,
) -> ApprovalOutcome:
    """Validate a presented grant against the server's own record.

    Order matters only for the reported reason, never for the decision: every
    branch fails closed. Caller responsibilities that this function deliberately
    does **not** take on, because they are structural rather than cryptographic:

    * finding ``record`` only within the authenticated user's session (that is
      what binds the approval to a user and a conversation), and
    * **atomically** burning it. The ``consumed`` check below reads a snapshot
      and is advisory only — two concurrent redeemers both see ``False``. The
      authoritative single-use decision is
      ``SessionRepository.consume_tool_approval``, a compare-and-set; a caller
      that does not win it must deny even though every check here passed.
    """
    if record is None:
        return ApprovalOutcome(reason=ApprovalDenied.unknown_request)
    if not isinstance(grant, str) or not grant:
        return ApprovalOutcome(reason=ApprovalDenied.bad_grant)
    if not hmac.compare_digest(grant_hash(grant), record.grantHash):
        return ApprovalOutcome(reason=ApprovalDenied.bad_grant)
    if record.consumed:
        return ApprovalOutcome(reason=ApprovalDenied.already_used)
    if record.is_expired(now=now):
        return ApprovalOutcome(reason=ApprovalDenied.expired)
    return ApprovalOutcome(record=record)


def invocation_approvals_for(records: Sequence[PendingToolApproval]) -> frozenset[str]:
    """Turn redeemed records into the runtime's per-invocation approval set."""
    return frozenset(record.key for record in records)
