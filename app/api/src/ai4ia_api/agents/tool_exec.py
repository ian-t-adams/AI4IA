"""Tool **execution** layer (Phase 4) — the counterpart to the safety registry.

``agents/tools.py`` *governs* tools (scopes, risk, approval, egress, redaction);
this module *executes* them. The two are joined only by a tool ``name`` so the
governance model stays centralized and framework-agnostic. A :class:`ToolDefinition`
bundles, at construction time, a tool's safety :class:`ToolSpec`, its JSON-Schema
``parameters`` (what the model is told it can pass), and an async/sync ``handler``.

The built-in tools here are intentionally **safe** (read-only, no network egress,
no secrets): a bounded arithmetic ``calculator`` and ``get_current_time``. Future
MCP / Foundry-toolbox / custom-Python adapters plug in as additional
:class:`ToolDefinition`s (or a richer :class:`ToolExecutor`) behind the same seam,
and remain subject to the same registry authorization on every call.
"""
from __future__ import annotations

import ast
import inspect
import operator
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .tools import ToolRegistry, ToolRisk, ToolSpec

# A handler maps validated arguments + context to a JSON-serializable result. It
# may be sync or async; :meth:`ToolExecutor.execute` awaits awaitables.
ToolHandler = Callable[[dict[str, Any], "ToolContext"], Any]


class ToolExecutionError(Exception):
    """A tool handler failed at runtime (surfaced to the model as a tool result)."""


class ToolValidationError(Exception):
    """Model-supplied arguments did not satisfy the tool's parameter schema."""


@dataclass(frozen=True)
class ToolContext:
    """Per-invocation authorization context passed to ``authorize`` + handlers.

    ``target_hosts`` is required by the registry's egress allowlist check; built-in
    tools reach no network and pass an empty set, but egress-capable tools must
    derive their target hosts and supply them so authorization can gate them.
    """

    granted_scopes: frozenset[str] = field(default_factory=frozenset)
    approvals: frozenset[str] = field(default_factory=frozenset)
    target_hosts: frozenset[str] = field(default_factory=frozenset)
    correlation_id: str | None = None


@dataclass(frozen=True)
class ToolDefinition:
    """A tool's safety contract + parameter schema + executable handler."""

    spec: ToolSpec
    parameters: dict[str, Any]
    handler: ToolHandler


# --- Minimal JSON-Schema argument validation -----------------------------------
# Deliberately tiny: enough to validate the simple object schemas the built-ins
# (and near-term tools) declare without taking a jsonschema dependency. Richer
# validation can adopt a real validator later without changing call sites.
_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _type_ok(value: Any, json_type: str) -> bool:
    expected = _JSON_TYPES.get(json_type)
    if expected is None:
        return True  # unknown declared type: don't block
    # In Python ``bool`` is an ``int``; never accept a bool for number/integer.
    if json_type in ("number", "integer") and isinstance(value, bool):
        return False
    return isinstance(value, expected)


def validate_args(schema: dict[str, Any], args: dict[str, Any]) -> list[str]:
    """Return a list of human-readable validation errors (empty == valid)."""
    errors: list[str] = []
    if not isinstance(args, dict):
        return ["arguments must be a JSON object"]
    properties: dict[str, Any] = schema.get("properties") or {}
    for required in schema.get("required") or []:
        if required not in args:
            errors.append(f"missing required argument: {required}")
    for key, value in args.items():
        prop = properties.get(key)
        if not isinstance(prop, dict):
            continue  # unknown/extra property: tolerate (additionalProperties)
        declared = prop.get("type")
        if isinstance(declared, str) and not _type_ok(value, declared):
            errors.append(f"argument {key!r} must be of type {declared}")
    return errors


# --- Safe bounded arithmetic for the `calculator` built-in ----------------------
_MAX_EXPR_LEN = 200
_MAX_AST_NODES = 100
_MAX_MAGNITUDE = 10**15  # bound literals and intermediate/final results

# Note: exponentiation (``**``) is intentionally unsupported — it is the easiest
# arithmetic DoS vector (e.g. ``9**9**9``) and is rarely needed for chat math.
_BIN_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}
_UNARY_OPS: dict[type, Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _check_magnitude(value: Any) -> Any:
    if isinstance(value, (int, float)) and abs(value) > _MAX_MAGNITUDE:
        raise ToolExecutionError("number out of range")
    return value


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ToolExecutionError("only numeric literals are allowed")
        return _check_magnitude(node.value)
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ToolExecutionError("unsupported operator")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        try:
            return _check_magnitude(op(left, right))
        except ZeroDivisionError as exc:
            raise ToolExecutionError("division by zero") from exc
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ToolExecutionError("unsupported unary operator")
        return _check_magnitude(op(_eval_node(node.operand)))
    raise ToolExecutionError("unsupported expression")


def safe_eval_arithmetic(expression: str) -> float:
    """Evaluate a basic arithmetic expression with no names, calls, or power.

    Allowed: integer/float literals, ``+ - * / // %``, parentheses, unary +/-.
    Bounded by expression length, AST node count, and value magnitude.
    """
    if len(expression) > _MAX_EXPR_LEN:
        raise ToolExecutionError("expression too long")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolExecutionError("could not parse expression") from exc
    if len(list(ast.walk(tree))) > _MAX_AST_NODES:
        raise ToolExecutionError("expression too complex")
    return _eval_node(tree.body)


def _calculator_handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    expression = args.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        raise ToolValidationError("expression must be a non-empty string")
    return {"expression": expression, "result": safe_eval_arithmetic(expression)}


def _current_time_handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return {"utc": datetime.now(timezone.utc).isoformat()}


def builtin_tools() -> list[ToolDefinition]:
    """The safe, dependency-free tools every deployment ships with."""
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="calculator",
                description=(
                    "Evaluate a basic arithmetic expression (+ - * / // %, parentheses, "
                    "unary minus). No variables, functions, or exponentiation."
                ),
                risk=ToolRisk.safe,
            ),
            parameters={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Arithmetic expression, e.g. (2 + 3) * 4",
                    }
                },
                "required": ["expression"],
            },
            handler=_calculator_handler,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="get_current_time",
                description="Return the current UTC date and time in ISO 8601 format.",
                risk=ToolRisk.safe,
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_current_time_handler,
        ),
    ]


class ToolExecutor:
    """Registry of executable :class:`ToolDefinition`s, joined to the safety
    registry by name. Exposes only authorized tools to the model and validates
    arguments before invoking a handler."""

    def __init__(self) -> None:
        self._defs: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        name = definition.spec.name
        if name in self._defs:
            raise ValueError(f"tool already registered: {name}")
        self._defs[name] = definition

    def get(self, name: str) -> ToolDefinition | None:
        return self._defs.get(name)

    def names(self) -> list[str]:
        """Names of every registered (executable) tool, sorted."""
        return sorted(self._defs)

    def schema_for(
        self,
        names: Iterable[str],
        *,
        registry: ToolRegistry,
        ctx: ToolContext,
    ) -> list[dict[str, Any]]:
        """OpenAI ``tools`` array for ``names`` that are executable AND currently
        authorized for ``ctx`` — so the model never sees a tool it cannot use
        (which would otherwise waste iterations and widen the attack surface)."""
        out: list[dict[str, Any]] = []
        for name in names:
            definition = self._defs.get(name)
            if definition is None:
                continue
            decision = registry.authorize(
                name,
                granted_scopes=ctx.granted_scopes,
                target_hosts=ctx.target_hosts,
                approved=name in ctx.approvals,
            )
            if not decision.allowed:
                continue
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": definition.spec.description,
                        "parameters": definition.parameters,
                    },
                }
            )
        return out

    async def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> Any:
        """Validate arguments against the tool schema, then run its handler.

        Raises :class:`ToolValidationError` for bad arguments and
        :class:`ToolExecutionError` (or whatever the handler raises) on failure;
        the runtime turns both into a structured tool result for the model.
        """
        definition = self._defs.get(name)
        if definition is None:
            raise ToolExecutionError(f"unknown tool: {name}")
        errors = validate_args(definition.parameters, args)
        if errors:
            raise ToolValidationError("; ".join(errors))
        result = definition.handler(args, ctx)
        if inspect.isawaitable(result):
            result = await result
        return result


def build_tools(extra: Iterable[ToolDefinition] = ()) -> tuple[ToolRegistry, ToolExecutor]:
    """Construct a paired safety registry + executor seeded with the built-ins.

    The registry and executor are separate objects (governance vs. execution);
    they are seeded together here so a tool's safety contract and its handler can
    never drift apart by name.
    """
    registry = ToolRegistry()
    executor = ToolExecutor()
    for definition in (*builtin_tools(), *extra):
        registry.register(definition.spec)
        executor.register(definition)
    return registry, executor


# Explicit, opt-in allowlist of tools a *user* may attach to a user-defined
# agent. A tool must be on this list AND independently pass the safety predicate
# below — so registering a new "safe" tool never silently makes it user-attachable
# (it would also have to be added here on purpose).
USER_ATTACHABLE_TOOL_NAMES: frozenset[str] = frozenset({"calculator", "get_current_time"})


# Synthetic, service-backed capabilities a *user* may attach to an agent. Unlike
# the builtin registry tools above, these are NOT executed through the registry
# executor (their handlers need real services + the authenticated user, which an
# empty agent-turn ``ToolContext`` cannot carry). The chat router builds and
# injects the backing capability per turn when the tool is present AND its
# services are available; if they are not, the agent simply runs without it. They
# are listed here so a user agent may reference them and the Agent Builder can
# offer them, governed by the same per-user agent validation as builtin tools.
SELECTABLE_SYNTHETIC_TOOL_NAMES: frozenset[str] = frozenset(
    {"generate_image", "generate_video", "process_document", "recall_memory"}
)


def attachable_tool_names(
    registry: ToolRegistry, executor: ToolExecutor
) -> frozenset[str]:
    """The effective allowlist of tools a *user* may attach to a user-defined agent.

    A user-attachable tool must be (0) on the explicit
    :data:`USER_ATTACHABLE_TOOL_NAMES` opt-in list, (1) executable (the executor
    has a handler), (2) registered + enabled + allowlisted in the safety registry,
    (3) ``safe`` risk, (4) require no approval, and (5) declare no required scopes,
    secret refs, or egress hosts. The last three matter because an agent turn runs
    with an empty :class:`ToolContext` (no granted scopes/approvals), so a tool
    needing any of those could never actually run — and we must never let a user
    attach a destructive/external/secret-bearing tool to a persona regardless. The
    explicit opt-in list is defense in depth: a future "safe" tool added for
    curated/internal agents does not automatically become user-attachable.

    The result also includes :data:`SELECTABLE_SYNTHETIC_TOOL_NAMES` — service-backed
    capabilities (e.g. ``generate_image``) that the chat router injects per turn
    rather than running through the executor. They are user-selectable but are not
    subject to the executor/registry predicate above (they have no registry entry).
    """
    out: set[str] = set(SELECTABLE_SYNTHETIC_TOOL_NAMES)
    for name in executor.names():
        if name not in USER_ATTACHABLE_TOOL_NAMES:
            continue
        spec = registry.get(name)
        if spec is None or not spec.enabled:
            continue
        if not registry.is_allowlisted(name):
            continue
        if spec.risk is not ToolRisk.safe or spec.needs_approval:
            continue
        if spec.scopes or spec.secret_refs or spec.egress_allowlist:
            continue
        out.add(name)
    return frozenset(out)
