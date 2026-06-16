"""Unit tests for the user-defined agent service, store, and tool allowlist."""
from __future__ import annotations

import pytest

from ai4ia_api.agents.agent_catalog import AgentCatalog, AgentSpec
from ai4ia_api.agents.service import AgentService
from ai4ia_api.agents.store import InMemoryUserAgentStore
from ai4ia_api.agents.tool_exec import attachable_tool_names, build_tools
from ai4ia_api.agents.user_agents import (
    MAX_AGENTS_PER_USER,
    AgentConflictError,
    AgentNotFoundError,
    AgentValidationError,
    UserAgentCreate,
    UserAgentUpdate,
)
from ai4ia_api.catalog import load_catalog

CURATED = AgentCatalog(
    agents=[
        AgentSpec(
            name="coder",
            displayName="Coder",
            description="writes code",
            systemPrompt="You write code.",
        )
    ]
)


def _service() -> tuple[AgentService, str]:
    catalog = load_catalog()
    registry, executor = build_tools()
    attachable = attachable_tool_names(registry, executor)
    service = AgentService(
        InMemoryUserAgentStore(), catalog=catalog, attachable_tools=attachable
    )
    model = catalog.models[0].id
    return service, model


def _curated_names() -> set[str]:
    return {a.name for a in CURATED.agents}


def test_attachable_tools_are_the_safe_builtins():
    registry, executor = build_tools()
    attachable = attachable_tool_names(registry, executor)
    # The seeded built-ins are both safe/no-scope/no-approval, so both qualify.
    # ``generate_image``, ``generate_video``, and ``process_document`` are
    # service-backed synthetic capabilities (no registry handler) seeded via
    # SELECTABLE_SYNTHETIC_TOOL_NAMES, so they are also offered.
    assert attachable == frozenset(
        {
            "calculator",
            "get_current_time",
            "generate_image",
            "generate_video",
            "process_document",
        }
    )


async def test_create_then_compose_into_catalog():
    service, _ = _service()
    await service.create(
        "u1",
        UserAgentCreate(
            name="pirate",
            displayName="Pirate",
            description="talks like a pirate",
            systemPrompt="Arr, speak like a pirate.",
        ),
        reserved_names=_curated_names(),
    )
    catalog = await service.catalog_for("u1", CURATED)
    assert catalog.get("pirate") is not None
    assert catalog.get("pirate").systemPrompt == "Arr, speak like a pirate."
    # Curated agents remain.
    assert catalog.get("coder") is not None


async def test_create_with_generate_image_tool_is_accepted():
    # ``generate_image`` is a service-backed synthetic capability offered via the
    # selectable allowlist, so an agent may compose it like a safe built-in.
    service, _ = _service()
    agent = await service.create(
        "u1",
        UserAgentCreate(
            name="illustrator",
            systemPrompt="You draw things.",
            tools=["generate_image"],
        ),
        reserved_names=_curated_names(),
    )
    assert "generate_image" in agent.tools


async def test_user_agents_are_per_user_isolated():
    service, _ = _service()
    await service.create(
        "u1",
        UserAgentCreate(name="pirate", systemPrompt="Arr."),
        reserved_names=_curated_names(),
    )
    u1 = await service.catalog_for("u1", CURATED)
    u2 = await service.catalog_for("u2", CURATED)
    assert u1.get("pirate") is not None
    assert u2.get("pirate") is None


async def test_curated_name_is_reserved():
    service, _ = _service()
    with pytest.raises(AgentConflictError):
        await service.create(
            "u1",
            UserAgentCreate(name="coder", systemPrompt="hi"),
            reserved_names=_curated_names(),
        )


async def test_curated_wins_on_collision_even_if_stored():
    """A user agent whose name collides with a curated one (e.g. created before a
    curated agent shipped) must never shadow the curated persona."""
    service, _ = _service()
    # Bypass create()'s reservation to simulate a pre-existing colliding record.
    store = service._store
    from ai4ia_api.agents.user_agents import UserAgent

    await store.put(
        UserAgent(id="coder", userId="u1", name="coder", displayName="X",
                  systemPrompt="EVIL")
    )
    catalog = await service.catalog_for("u1", CURATED)
    assert catalog.get("coder").systemPrompt == "You write code."


async def test_duplicate_name_for_same_user_conflicts():
    service, _ = _service()
    await service.create(
        "u1", UserAgentCreate(name="pirate", systemPrompt="Arr."),
        reserved_names=_curated_names(),
    )
    with pytest.raises(AgentConflictError):
        await service.create(
            "u1", UserAgentCreate(name="pirate", systemPrompt="Arr2."),
            reserved_names=_curated_names(),
        )


async def test_per_user_cap_enforced():
    service, _ = _service()
    for i in range(MAX_AGENTS_PER_USER):
        await service.create(
            "u1", UserAgentCreate(name=f"a{i}", systemPrompt="hi"),
            reserved_names=_curated_names(),
        )
    with pytest.raises(AgentConflictError):
        await service.create(
            "u1", UserAgentCreate(name="overflow", systemPrompt="hi"),
            reserved_names=_curated_names(),
        )


@pytest.mark.parametrize("bad", ["", "1pirate", "pi rate", "pirate-", "x" * 33])
async def test_invalid_names_rejected(bad):
    service, _ = _service()
    with pytest.raises(AgentValidationError):
        await service.create(
            "u1", UserAgentCreate(name=bad, systemPrompt="hi"),
            reserved_names=_curated_names(),
        )


async def test_empty_system_prompt_rejected():
    service, _ = _service()
    with pytest.raises(AgentValidationError):
        await service.create(
            "u1", UserAgentCreate(name="pirate", systemPrompt="   "),
            reserved_names=_curated_names(),
        )


async def test_unknown_model_rejected():
    service, _ = _service()
    with pytest.raises(AgentValidationError):
        await service.create(
            "u1",
            UserAgentCreate(name="pirate", systemPrompt="hi", defaultModel="no-such"),
            reserved_names=_curated_names(),
        )


async def test_known_model_accepted():
    service, model = _service()
    agent = await service.create(
        "u1",
        UserAgentCreate(name="pirate", systemPrompt="hi", defaultModel=model),
        reserved_names=_curated_names(),
    )
    assert agent.defaultModel == model


async def test_unattachable_tool_rejected():
    service, _ = _service()
    with pytest.raises(AgentValidationError):
        await service.create(
            "u1",
            UserAgentCreate(name="pirate", systemPrompt="hi", tools=["rm_rf"]),
            reserved_names=_curated_names(),
        )


async def test_attachable_tool_accepted_and_deduped():
    service, _ = _service()
    agent = await service.create(
        "u1",
        UserAgentCreate(name="pirate", systemPrompt="hi", tools=["calculator"]),
        reserved_names=_curated_names(),
    )
    assert agent.tools == ["calculator"]
    with pytest.raises(AgentValidationError):
        await service.create(
            "u2",
            UserAgentCreate(
                name="dup", systemPrompt="hi", tools=["calculator", "calculator"]
            ),
            reserved_names=_curated_names(),
        )


async def test_owned_mcp_tool_name_is_attachable_when_supplied():
    """When the router supplies the caller's own discovered MCP tool names (the
    feature is on), an agent may attach an ``mcp:<server>/<tool>`` name."""
    service, _ = _service()
    agent = await service.create(
        "u1",
        UserAgentCreate(
            name="weatherbot",
            systemPrompt="You report the weather.",
            tools=["calculator", "mcp:weather/forecast"],
        ),
        reserved_names=_curated_names(),
        mcp_tool_names={"mcp:weather/forecast"},
    )
    assert agent.tools == ["calculator", "mcp:weather/forecast"]


async def test_unowned_mcp_tool_name_is_rejected():
    """An ``mcp:*`` name the caller does not own is rejected even when the feature
    is on (the router only lists the caller's own servers)."""
    service, _ = _service()
    with pytest.raises(AgentValidationError):
        await service.create(
            "u1",
            UserAgentCreate(
                name="weatherbot",
                systemPrompt="hi",
                tools=["mcp:someone-else/secret"],
            ),
            reserved_names=_curated_names(),
            mcp_tool_names={"mcp:weather/forecast"},
        )


async def test_mcp_tool_name_rejected_when_feature_off():
    """When custom tools are off the router supplies no MCP names, so an
    ``mcp:*`` name falls through to the rejection exactly as before."""
    service, _ = _service()
    with pytest.raises(AgentValidationError):
        await service.create(
            "u1",
            UserAgentCreate(
                name="weatherbot",
                systemPrompt="hi",
                tools=["mcp:weather/forecast"],
            ),
            reserved_names=_curated_names(),
            # mcp_tool_names omitted (None) -> behaves like the feature being off.
        )


async def test_update_admits_owned_mcp_tool_name():
    service, _ = _service()
    await service.create(
        "u1",
        UserAgentCreate(name="weatherbot", systemPrompt="hi"),
        reserved_names=_curated_names(),
    )
    updated = await service.update(
        "u1",
        "weatherbot",
        UserAgentUpdate(
            systemPrompt="now with tools",
            tools=["mcp:weather/forecast"],
        ),
        mcp_tool_names={"mcp:weather/forecast"},
    )
    assert updated.tools == ["mcp:weather/forecast"]


async def test_update_replaces_fields_and_keeps_created_at():
    service, _ = _service()
    created = await service.create(
        "u1", UserAgentCreate(name="pirate", systemPrompt="Arr."),
        reserved_names=_curated_names(),
    )
    updated = await service.update(
        "u1", "pirate", UserAgentUpdate(systemPrompt="Yarr.", enabled=False)
    )
    assert updated.systemPrompt == "Yarr."
    assert updated.enabled is False
    assert updated.createdAt == created.createdAt
    assert updated.updatedAt >= created.updatedAt


async def test_update_missing_raises_not_found():
    service, _ = _service()
    with pytest.raises(AgentNotFoundError):
        await service.update("u1", "ghost", UserAgentUpdate(systemPrompt="hi"))


async def test_delete_is_idempotent():
    service, _ = _service()
    await service.create(
        "u1", UserAgentCreate(name="pirate", systemPrompt="Arr."),
        reserved_names=_curated_names(),
    )
    await service.delete("u1", "pirate")
    await service.delete("u1", "pirate")  # no error
    catalog = await service.catalog_for("u1", CURATED)
    assert catalog.get("pirate") is None


async def test_compose_fails_open_on_store_error():
    catalog = load_catalog()
    registry, executor = build_tools()

    class BoomStore(InMemoryUserAgentStore):
        async def list(self, user_id):  # noqa: ARG002
            raise RuntimeError("cosmos down")

    service = AgentService(
        BoomStore(), catalog=catalog,
        attachable_tools=attachable_tool_names(registry, executor),
    )
    result = await service.catalog_for("u1", CURATED)
    # Fails open: curated catalog is returned unchanged.
    assert result is CURATED
