from ai4ia_api.agents.tools import (
    DenyReason,
    ToolRegistry,
    ToolRisk,
    ToolSpec,
    is_safe_tool_name,
    redact,
    redact_obj,
)


def _reg(*specs: ToolSpec, allowlist=None) -> ToolRegistry:
    reg = ToolRegistry(allowlist=allowlist)
    for s in specs:
        reg.register(s)
    return reg


def test_register_and_list_sorted():
    reg = _reg(
        ToolSpec(name="zeta", description="z"),
        ToolSpec(name="alpha", description="a"),
    )
    assert [t.name for t in reg.list()] == ["alpha", "zeta"]


def test_duplicate_registration_raises():
    reg = _reg(ToolSpec(name="dup", description="d"))
    try:
        reg.register(ToolSpec(name="dup", description="d2"))
    except ValueError as exc:
        assert "dup" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_authorize_safe_tool_allows():
    reg = _reg(ToolSpec(name="search", description="web search"))
    decision = reg.authorize("search")
    assert decision.allowed
    assert decision.reason is None


def test_unknown_tool_denied():
    reg = _reg()
    decision = reg.authorize("nope")
    assert decision.denied
    assert decision.reason is DenyReason.unknown_tool


def test_disabled_tool_denied():
    reg = _reg(ToolSpec(name="t", description="d", enabled=False))
    assert reg.authorize("t").reason is DenyReason.disabled


def test_allowlist_blocks_registered_but_unlisted_tool():
    reg = _reg(
        ToolSpec(name="a", description="d"),
        ToolSpec(name="b", description="d"),
        allowlist=["a"],
    )
    assert reg.authorize("a").allowed
    assert reg.authorize("b").reason is DenyReason.not_allowlisted


def test_missing_scopes_denied_with_detail():
    reg = _reg(ToolSpec(name="db", description="d", scopes=frozenset({"db:write"})))
    decision = reg.authorize("db", granted_scopes=["chat"])
    assert decision.denied
    assert decision.reason is DenyReason.missing_scopes
    assert decision.missing_scopes == frozenset({"db:write"})


def test_scopes_satisfied_allows():
    reg = _reg(ToolSpec(name="db", description="d", scopes=frozenset({"db:write"})))
    assert reg.authorize("db", granted_scopes=["db:write", "chat"]).allowed


def test_egress_allowlist_blocks_foreign_host():
    reg = _reg(
        ToolSpec(
            name="fetch",
            description="d",
            risk=ToolRisk.external,
            egress_allowlist=frozenset({"api.bing.microsoft.com"}),
        )
    )
    ok = reg.authorize("fetch", target_hosts=["api.bing.microsoft.com"])
    assert ok.allowed
    bad = reg.authorize("fetch", target_hosts=["evil.example.com"])
    assert bad.reason is DenyReason.egress_blocked
    assert bad.blocked_hosts == frozenset({"evil.example.com"})


def test_destructive_tool_requires_approval():
    reg = _reg(ToolSpec(name="rm", description="d", risk=ToolRisk.destructive))
    assert reg.authorize("rm").reason is DenyReason.approval_required
    assert reg.authorize("rm", approved=True).allowed


def test_requires_approval_flag_gates_even_safe_tool():
    reg = _reg(ToolSpec(name="email", description="d", requires_approval=True))
    assert reg.authorize("email").reason is DenyReason.approval_required
    assert reg.authorize("email", approved=True).allowed


def test_needs_approval_property():
    assert ToolSpec(name="x", description="d", risk=ToolRisk.destructive).needs_approval
    assert ToolSpec(name="x", description="d", requires_approval=True).needs_approval
    assert not ToolSpec(name="x", description="d").needs_approval


def test_redact_key_value_pairs():
    out = redact("api_key=ghp_abc connecting with token: secretvalue123")
    assert "ghp_abc" not in out
    assert "***REDACTED***" in out


def test_redact_long_opaque_token():
    token = "A" * 40
    assert token not in redact(f"using {token} now")


def test_redact_leaves_short_words_alone():
    assert redact("hello world 1/2") == "hello world 1/2"


def test_redact_obj_masks_secret_keys_and_nested():
    payload = {
        "model": "gpt-5.2",
        "api_key": "supersecret",
        "nested": {"password": "p", "note": "fine"},
        "items": ["ok", "token=abcdef0123456789abcdef0123456789xx"],
    }
    out = redact_obj(payload)
    assert out["model"] == "gpt-5.2"
    assert out["api_key"] == "***REDACTED***"
    assert out["nested"]["password"] == "***REDACTED***"
    assert out["nested"]["note"] == "fine"
    assert "abcdef0123456789" not in out["items"][1]


def test_is_safe_tool_name_accepts_every_current_builtin_and_synthetic_name():
    # Every name this codebase actually registers today (see tool_exec.py's
    # builtins and runtime.py's delegate_to_agent synthetic capability) must
    # remain accepted -- the contract must never regress a legitimate name.
    for name in (
        "calculator",
        "get_current_time",
        "web_search",
        "news_search",
        "video_search",
        "image_search",
        "browse_url",
        "run_code",
        "export_document",
        "fetch_document",
        "process_document",
        "analyze_attachment",
        "generate_image",
        "generate_video",
        "recall_memory",
        "delegate_to_agent",
    ):
        assert is_safe_tool_name(name), name


def test_is_safe_tool_name_accepts_bounded_hyphenated_dynamic_names():
    # A plausible dynamic (non-namespaced) tool name: hyphens, mixed case, digits.
    assert is_safe_tool_name("my-server-Tool_2")
    assert is_safe_tool_name("a")
    assert is_safe_tool_name("a" * 64)


def test_is_safe_tool_name_accepts_well_formed_mcp_namespaced_names():
    # Regression: MCP tools are dispatched under the pre-existing canonical
    # "mcp:<server>/<tool>" form (see mcp_servers.namespaced_tool_name). A
    # bounded-charset check that only recognized bare names used to sentinel
    # every real MCP tool call to "unknown_tool" in logs/activity, since the
    # namespaced form legitimately contains ":" and "/". These must resolve
    # as safe so a genuine MCP call's real name reaches logs/activity.
    assert is_safe_tool_name("mcp:a/ta")
    assert is_safe_tool_name("mcp:my-server_1/get-weather_2")
    assert is_safe_tool_name(f"mcp:{'s' * 32}/{'t' * 64}")


def test_is_safe_tool_name_rejects_malformed_mcp_namespaced_names():
    for hostile in (
        "mcp:a/tool\nname",  # newline smuggled into the tool component
        "mcp:a/tool/extra",  # an extra "/" -- not the canonical shape
        "mcp:/ta",  # empty server component
        "mcp:a/",  # empty tool component
        "mcp:a\n/ta",  # newline smuggled into the server component
        f"mcp:{'s' * 33}/t",  # server component over the bound
        f"mcp:a/{'t' * 65}",  # tool component over the bound
        "mcpx:a/ta",  # not actually the "mcp:" prefix
    ):
        assert not is_safe_tool_name(hostile), hostile


def test_is_safe_tool_name_rejects_malformed_or_hostile_names():
    for hostile in (
        "",
        "a" * 65,
        "tool\nwith\nnewline",
        "tool with spaces",
        "tool\twith\ttab",
        "tool.with.dots",
        "tool/with/slash",
        "tool\x00null",
        "emoji\U0001f600",
        "quoted\"tool",
    ):
        assert not is_safe_tool_name(hostile), hostile
