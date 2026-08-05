"""Tool-safety registry.

Every tool an agent can call is declared here with explicit safety metadata:
the scopes a caller must hold, whether the tool is destructive or reaches
external networks, whether it requires human approval, which secrets it may
read, and an egress host allowlist. The registry *authorizes* invocations
against a caller's granted scopes and *redacts* secret-looking values from any
logged tool I/O.

This module is framework-agnostic: it does not execute tools, it governs them.
Execution adapters (MCP, Foundry toolbox, custom Python) plug in on top and are
expected to call :meth:`ToolRegistry.authorize` before every invocation and to
route all logged I/O through :func:`redact` / :func:`redact_obj`.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ToolRisk(str, Enum):
    safe = "safe"  # read-only, no external egress
    external = "external"  # reaches third-party networks
    destructive = "destructive"  # mutates or destroys state


class DenyReason(str, Enum):
    unknown_tool = "unknown_tool"
    disabled = "disabled"
    not_allowlisted = "not_allowlisted"
    missing_scopes = "missing_scopes"
    egress_blocked = "egress_blocked"
    approval_required = "approval_required"


@dataclass(frozen=True)
class ToolSpec:
    """Declarative safety contract for a single tool."""

    name: str
    description: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    risk: ToolRisk = ToolRisk.safe
    requires_approval: bool = False
    secret_refs: frozenset[str] = field(default_factory=frozenset)
    egress_allowlist: frozenset[str] = field(default_factory=frozenset)
    enabled: bool = True

    @property
    def needs_approval(self) -> bool:
        """Destructive tools always require approval, as do explicitly flagged ones."""
        return self.requires_approval or self.risk is ToolRisk.destructive


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    tool: str
    reason: DenyReason | None = None
    missing_scopes: frozenset[str] = field(default_factory=frozenset)
    blocked_hosts: frozenset[str] = field(default_factory=frozenset)

    @property
    def denied(self) -> bool:
        return not self.allowed


class ToolRegistry:
    """In-memory registry of :class:`ToolSpec` with allowlist + scope enforcement."""

    def __init__(self, *, allowlist: Iterable[str] | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        # When set, only these tool names may be authorized even if registered.
        # ``None`` means every registered tool is implicitly allowlisted.
        self._allowlist: set[str] | None = (
            set(allowlist) if allowlist is not None else None
        )

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def list(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def is_allowlisted(self, name: str) -> bool:
        return self._allowlist is None or name in self._allowlist

    def authorize(
        self,
        name: str,
        *,
        granted_scopes: Iterable[str] = (),
        target_hosts: Iterable[str] = (),
        approved: bool = False,
    ) -> AuthorizationDecision:
        """Decide whether a caller may invoke ``name``.

        Checks, in order: existence, enabled, allowlist, required scopes, egress
        allowlist (only enforced when the tool declares one and hosts are given),
        and human-approval gating.
        """
        spec = self._tools.get(name)
        if spec is None:
            return AuthorizationDecision(False, name, DenyReason.unknown_tool)
        if not spec.enabled:
            return AuthorizationDecision(False, name, DenyReason.disabled)
        if not self.is_allowlisted(name):
            return AuthorizationDecision(False, name, DenyReason.not_allowlisted)

        missing = spec.scopes - set(granted_scopes)
        if missing:
            return AuthorizationDecision(
                False, name, DenyReason.missing_scopes, missing_scopes=frozenset(missing)
            )

        hosts = list(target_hosts)
        if spec.egress_allowlist and hosts:
            blocked = {h for h in hosts if h not in spec.egress_allowlist}
            if blocked:
                return AuthorizationDecision(
                    False, name, DenyReason.egress_blocked, blocked_hosts=frozenset(blocked)
                )

        if spec.needs_approval and not approved:
            return AuthorizationDecision(False, name, DenyReason.approval_required)

        return AuthorizationDecision(True, name)


# --- Redaction of secret-looking values in logged tool I/O ---------------------

_REDACTED = "***REDACTED***"
# key=value / key: value pairs where the key names a credential.
_KV_SECRET_RE = re.compile(
    # Label alternation covers the names this stack actually carries, not only
    # generic ones: APIM's `Ocp-Apim-Subscription-Key` and the bare
    # `subscription-key` form appear in gateway error bodies, and the proxy's
    # `S7P-KEY` in proxy errors. None of them contains a substring that
    # `api[_-]?key` matches.
    r"(?i)\b(api[_-]?key|subscription[_-]?key|ocp-apim-subscription-key|s7p-key"
    r"|secret|token|password|passwd|authorization|client[_-]?secret)\b"
    # `\"?` is load-bearing. In JSON the label's own closing quote sits between
    # the name and the colon (`"api_key": "..."`), and `\s*[=:]` cannot cross it,
    # so without this the most common shape a credential arrives in -- a decoded
    # JSON error body from the API, the gateway or an MCP server -- was not
    # matched at all. `_LONG_TOKEN_RE` masked the >=32-char cases (real APIM
    # keys, JWTs) and hid how wide the gap was; short credentials such as a
    # user's BYO MCP password or a base64 basic-auth blob passed through intact.
    r"(\"?\s*[=:]\s*\"?)([^\"\s,;}]+)"
)
# Opaque high-entropy tokens (PATs, keys). Dotted values like JWTs are redacted
# segment-by-segment, which still removes the sensitive material.
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")
# Key names (e.g. dict keys) that should have their value redacted wholesale.
_SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passwd|authorization|credential)"
)


def redact(text: str) -> str:
    """Mask credential key/value pairs and long opaque tokens in a string."""
    out = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", text)
    return _LONG_TOKEN_RE.sub(_REDACTED, out)


def redact_obj(value: Any) -> Any:
    """Recursively redact secrets from JSON-like tool I/O.

    Mapping values whose *key* looks like a credential are fully masked; all
    strings are run through :func:`redact`.
    """
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                out[k] = _REDACTED
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(value, (list, tuple)):
        seq = [redact_obj(v) for v in value]
        return type(value)(seq)
    if isinstance(value, str):
        return redact(value)
    return value


def is_fully_masked(value: Any) -> bool:
    """Whether :func:`redact_obj` replaced this value wholesale.

    A wholly-masked value means "hidden from you, but sent in full" — which a
    human-facing surface (see :mod:`ai4ia_api.agents.approvals`) must present
    differently from a value it is showing verbatim. Exposed as a predicate so
    the definition of "masked" lives here with the redactor rather than being
    re-derived by every caller that needs to tell the two states apart.
    """
    return value == _REDACTED


# --- Tool-name safety (defense against forged log/activity entries) ------------

# A tool *name* can originate from a source this codebase does not author or
# code-review (e.g. a name a remote MCP server advertises when its tools are
# discovered/registered). Being registered -- a key in the executor's handler
# map or a name known to this registry -- is proof the name is *dispatchable*,
# but it is NOT proof the name is safe to place in a free-text log line or a
# persisted activity record: nothing stops an untrusted registration from using
# a name containing newlines or other content crafted to forge a different log
# line. This bounded, canonical charset mirrors what OpenAI-style tool-calling
# function names already require, so no legitimate built-in, synthetic, or
# well-formed dynamic tool name is ever excluded by it.
_SAFE_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# MCP tools use the pre-existing, already-canonical namespaced form
# ``mcp:<server>/<tool>`` (see ``agents.mcp_servers.namespaced_tool_name``).
# The ``<server>`` slug is validated against a safe, bounded charset when the
# server is registered, but ``<tool>`` is supplied by the remote MCP server
# itself the same way a bare tool name would be, so it is held to the same
# bounded charset as any other dynamic name. A name that merely starts with
# "mcp:" but doesn't otherwise match this shape (extra "/" segments, empty
# components, control characters) is deliberately NOT matched here and falls
# through to the sentinel, since it is not the well-formed name this codebase
# itself would ever produce.
_SAFE_MCP_TOOL_NAME_RE = re.compile(r"mcp:[A-Za-z0-9_.-]{1,32}/[A-Za-z0-9_-]{1,64}")


def is_safe_tool_name(name: str) -> bool:
    """Whether ``name`` is safe to surface verbatim in logs or persisted activity.

    Independent of whether ``name`` is *registered*: a name can be genuinely
    registered/dispatchable and still fail this check (e.g. a dynamically
    discovered name that was never validated against a safe charset at
    registration time). Callers should keep dispatching/authorizing on the raw
    name regardless of this result -- it only gates what may be logged/persisted.
    """
    return bool(_SAFE_TOOL_NAME_RE.fullmatch(name) or _SAFE_MCP_TOOL_NAME_RE.fullmatch(name))
