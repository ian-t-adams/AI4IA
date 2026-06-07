import pytest

from ai4ia_api.agents.tool_exec import (
    ToolContext,
    ToolExecutionError,
    ToolExecutor,
    ToolValidationError,
    build_tools,
    safe_eval_arithmetic,
    validate_args,
)
from ai4ia_api.agents.tools import ToolRegistry, ToolRisk, ToolSpec


def _ctx(**kw) -> ToolContext:
    return ToolContext(**kw)


# --- calculator correctness + safety -------------------------------------------


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("2+2", 4),
        ("(2 + 3) * 4", 20),
        ("10 / 4", 2.5),
        ("7 % 3", 1),
        ("7 // 2", 3),
        ("-(3 + 4)", -7),
        ("2 * -3", -6),
    ],
)
def test_calculator_evaluates_basic_arithmetic(expr, expected):
    assert safe_eval_arithmetic(expr) == expected


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os')",  # name / call
        "os.system('x')",
        "2 ** 30",  # exponentiation is intentionally unsupported
        "9**9**9",
        "x + 1",  # free variable
        "abs(-1)",  # function call
        "1 if True else 2",  # non-arithmetic node
        "[1, 2]",  # list literal
    ],
)
def test_calculator_rejects_unsafe_or_unsupported(expr):
    with pytest.raises(ToolExecutionError):
        safe_eval_arithmetic(expr)


def test_calculator_rejects_division_by_zero():
    with pytest.raises(ToolExecutionError):
        safe_eval_arithmetic("1/0")


def test_calculator_bounds_magnitude():
    with pytest.raises(ToolExecutionError):
        safe_eval_arithmetic("99999999 * 99999999")


def test_calculator_bounds_expression_length():
    with pytest.raises(ToolExecutionError):
        safe_eval_arithmetic("1+" * 500 + "1")


# --- validate_args -------------------------------------------------------------


def test_validate_args_flags_missing_required():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    assert validate_args(schema, {}) == ["missing required argument: a"]


def test_validate_args_type_mismatch_and_bool_not_number():
    schema = {
        "type": "object",
        "properties": {"n": {"type": "number"}, "s": {"type": "string"}},
    }
    errs = validate_args(schema, {"n": True, "s": 5})
    assert any("n" in e for e in errs)
    assert any("s" in e for e in errs)


def test_validate_args_tolerates_extra_properties():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    assert validate_args(schema, {"a": "x", "extra": 1}) == []


# --- executor: schema filtering + execution ------------------------------------


def test_schema_for_exposes_only_authorized_executable_tools():
    registry, executor = build_tools()
    ctx = _ctx()
    schema = executor.schema_for(
        ["calculator", "get_current_time", "nonexistent"], registry=registry, ctx=ctx
    )
    names = {t["function"]["name"] for t in schema}
    assert names == {"calculator", "get_current_time"}
    # shape sanity
    calc = next(t for t in schema if t["function"]["name"] == "calculator")
    assert calc["type"] == "function"
    assert "expression" in calc["function"]["parameters"]["properties"]


def test_schema_for_hides_tool_missing_required_scope():
    # A tool requiring a scope the context lacks must not be advertised.
    registry, executor = build_tools()
    from ai4ia_api.agents.tool_exec import ToolDefinition

    scoped = ToolDefinition(
        spec=ToolSpec(name="scoped", description="needs scope", scopes=frozenset({"db:write"})),
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda args, ctx: {"ok": True},
    )
    registry.register(scoped.spec)
    executor.register(scoped)

    assert executor.schema_for(["scoped"], registry=registry, ctx=_ctx()) == []
    granted = _ctx(granted_scopes=frozenset({"db:write"}))
    assert len(executor.schema_for(["scoped"], registry=registry, ctx=granted)) == 1


def test_schema_for_hides_approval_required_tool_until_approved():
    registry, executor = build_tools()
    from ai4ia_api.agents.tool_exec import ToolDefinition

    risky = ToolDefinition(
        spec=ToolSpec(name="wipe", description="d", risk=ToolRisk.destructive),
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda args, ctx: {"ok": True},
    )
    registry.register(risky.spec)
    executor.register(risky)

    assert executor.schema_for(["wipe"], registry=registry, ctx=_ctx()) == []
    approved = _ctx(approvals=frozenset({"wipe"}))
    assert len(executor.schema_for(["wipe"], registry=registry, ctx=approved)) == 1


async def test_execute_runs_calculator():
    _registry, executor = build_tools()
    result = await executor.execute("calculator", {"expression": "6*7"}, _ctx())
    assert result == {"expression": "6*7", "result": 42}


async def test_execute_validates_before_running():
    _registry, executor = build_tools()
    with pytest.raises(ToolValidationError):
        await executor.execute("calculator", {}, _ctx())  # missing required


async def test_execute_unknown_tool_raises():
    _registry, executor = build_tools()
    with pytest.raises(ToolExecutionError):
        await executor.execute("ghost", {}, _ctx())


async def test_execute_supports_async_handler():
    from ai4ia_api.agents.tool_exec import ToolDefinition

    registry = ToolRegistry()
    executor = ToolExecutor()

    async def handler(args, ctx):
        return {"echoed": args.get("x")}

    d = ToolDefinition(
        spec=ToolSpec(name="aecho", description="d"),
        parameters={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        handler=handler,
    )
    registry.register(d.spec)
    executor.register(d)
    assert await executor.execute("aecho", {"x": "hi"}, _ctx()) == {"echoed": "hi"}


def test_build_tools_registers_both_layers_by_name():
    registry, executor = build_tools()
    for name in ("calculator", "get_current_time"):
        assert registry.get(name) is not None
        assert executor.get(name) is not None


def test_get_current_time_shape():
    _registry, executor = build_tools()
    definition = executor.get("get_current_time")
    out = definition.handler({}, _ctx())
    assert "utc" in out and out["utc"].endswith("+00:00")
