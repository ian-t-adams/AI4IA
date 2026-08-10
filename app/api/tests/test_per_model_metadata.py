"""Per-model metadata + scaling.

Covers: catalog metadata round-trips catalog -> API -> serialization; a model
without metadata falls back to the fixed constants; the request max-output is
capped per model (lower-only) and still composes with the gateway's reasoning
param translation; the document budget scales from the context window.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai4ia_api.catalog import DeploymentOption, ModelEntry, load_catalog
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.routers.chat import (
    DOC_CONTEXT_BUDGET,
    DOC_CONTEXT_BUDGET_MAX,
    GLOBAL_DEFAULT_MAX_TOKENS,
    MESSAGE_ENVELOPE_RESERVE_BYTES,
    TOOL_CONTEXT_RESERVE_TOKENS,
    _bound_payload_history,
    _doc_budget_for,
    _effective_params,
    _message_budget_bytes,
    _prompt_byte_budget,
)

from .conftest import make_settings

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INFRA_MODELS = _REPO_ROOT / "infra" / "models.json"

# The metadata-plumbing tests below assert against infra/models.json rather than a
# literal window size. The literal was a maintenance trap: a model's published
# context window is upstream data that changes on vendor revisions, so pinning it
# here made an unrelated `Add a model` catalog edit fail tests that are really
# about serialization. Reading the source of truth keeps the assertion exact and
# additionally catches build-time generator drift.
def _infra_metadata(name: str) -> tuple[int, int]:
    catalog = json.loads(_INFRA_MODELS.read_text(encoding="utf-8"))["catalog"]
    entry = next(m for m in catalog if m["name"] == name)
    return entry["contextWindow"], entry["maxOutputTokens"]


def _entry(**over) -> ModelEntry:
    base = dict(
        id="m",
        displayName="M",
        category="chat",
        format="OpenAI",
        options=[DeploymentOption(region="eastus2", sku="GlobalStandard", deploymentName="m-x")],
    )
    base.update(over)
    return ModelEntry(**base)


# --- A. metadata round-trip + fallback ----------------------------------------


def test_catalog_carries_metadata_for_known_model():
    ctx, out = _infra_metadata("gpt-5.4")
    catalog = load_catalog()
    entry = catalog.get("gpt-5.4")
    assert entry is not None
    assert entry.contextWindow == ctx
    assert entry.maxOutputTokens == out


def test_catalog_metadata_serializes_both_fields():
    ctx, out = _infra_metadata("gpt-5.4")
    catalog = load_catalog()
    dumped = catalog.get("gpt-5.4").model_dump()
    assert dumped["contextWindow"] == ctx
    assert dumped["maxOutputTokens"] == out


def test_model_without_metadata_falls_back_to_none():
    catalog = load_catalog()
    entry = catalog.get("model-router")
    assert entry is not None
    assert entry.contextWindow is None
    assert entry.maxOutputTokens is None
    dumped = entry.model_dump()
    assert dumped["contextWindow"] is None
    assert dumped["maxOutputTokens"] is None


def test_models_api_exposes_metadata(client):
    ctx, out = _infra_metadata("gpt-5.4")
    resp = client.get("/api/models", headers={"X-Dev-User": "alice"})
    assert resp.status_code == 200
    by_id = {m["id"]: m for m in resp.json()["models"]}
    assert by_id["gpt-5.4"]["contextWindow"] == ctx
    assert by_id["gpt-5.4"]["maxOutputTokens"] == out
    assert by_id["model-router"]["contextWindow"] is None
    assert by_id["model-router"]["maxOutputTokens"] is None


# --- B. max-output cap (lower-only) -------------------------------------------


def test_effective_params_unchanged_when_no_metadata():
    entry = _entry(maxOutputTokens=None)
    params = {"max_tokens": 999999, "temperature": 0.7}
    assert _effective_params(params, entry) == params
    assert _effective_params(params, None) == params


def test_effective_params_adopts_model_max_on_global_default():
    entry = _entry(maxOutputTokens=128000)
    out = _effective_params({"max_tokens": GLOBAL_DEFAULT_MAX_TOKENS}, entry)
    assert out["max_tokens"] == 128000


def test_effective_params_adopts_model_max_when_unset():
    entry = _entry(maxOutputTokens=64000)
    out = _effective_params({"temperature": 0.5}, entry)
    assert out["max_tokens"] == 64000
    assert out["temperature"] == 0.5


def test_effective_params_caps_too_high_request_down():
    entry = _entry(maxOutputTokens=32000)
    out = _effective_params({"max_tokens": 999999}, entry)
    assert out["max_tokens"] == 32000


def test_effective_params_leaves_lower_request_untouched():
    entry = _entry(maxOutputTokens=128000)
    out = _effective_params({"max_tokens": 4096}, entry)
    assert out["max_tokens"] == 4096


def test_effective_params_does_not_mutate_input():
    entry = _entry(maxOutputTokens=32000)
    params = {"max_tokens": 999999}
    _effective_params(params, entry)
    assert params["max_tokens"] == 999999


# --- B. cap composes with the gateway reasoning translation -------------------


def test_cap_then_reasoning_translation():
    """A capped max_tokens still becomes max_completion_tokens for gpt-5/o-series."""
    entry = _entry(id="gpt-5.4", maxOutputTokens=128000)
    eff = _effective_params({"max_tokens": GLOBAL_DEFAULT_MAX_TOKENS}, entry)
    gw = ModelGatewayClient(make_settings())
    req = gw.build_request(
        deployment="gpt-5.4-slurmfactory-eastus2-glbl",
        messages=[{"role": "user", "content": "hi"}],
        params=eff,
    )
    assert "max_tokens" not in req.json
    assert req.json["max_completion_tokens"] == 128000


def test_cap_keeps_max_tokens_for_non_reasoning_deployment():
    entry = _entry(id="Mistral-Large-3", maxOutputTokens=32768)
    eff = _effective_params({"max_tokens": 999999}, entry)
    gw = ModelGatewayClient(make_settings())
    req = gw.build_request(
        deployment="Mistral-Large-3-slurmfactory-eastus2-glbl",
        messages=[{"role": "user", "content": "hi"}],
        params=eff,
    )
    assert req.json["max_tokens"] == 32768
    assert "max_completion_tokens" not in req.json


# --- B. document budget scales from the context window ------------------------


def test_doc_budget_falls_back_without_metadata():
    assert _doc_budget_for(None) == DOC_CONTEXT_BUDGET
    assert _doc_budget_for(_entry(contextWindow=None)) == DOC_CONTEXT_BUDGET


def test_doc_budget_clamped_to_max_for_huge_window():
    assert _doc_budget_for(_entry(contextWindow=400000)) == DOC_CONTEXT_BUDGET_MAX


def test_doc_budget_scales_between_bounds():
    # 100_000 * 4 * 0.10 = 40_000, between 12_000 and 48_000.
    assert _doc_budget_for(_entry(contextWindow=100000)) == 40000


def test_doc_budget_floored_at_constant_for_small_window():
    # 20_000 * 4 * 0.10 = 8_000, floored up to the fixed constant.
    assert _doc_budget_for(_entry(contextWindow=20000)) == DOC_CONTEXT_BUDGET


# --- C. hard prompt/history budget --------------------------------------------


def test_prompt_budget_reserves_output_and_tool_capacity_from_catalog_window():
    entry = _entry(contextWindow=20_000, maxOutputTokens=2_000)
    params = _effective_params({}, entry)
    assert _prompt_byte_budget(entry, params) == (
        20_000 - 2_000 - TOOL_CONTEXT_RESERVE_TOKENS
    )


def test_metadata_free_model_fallback_does_not_narrow_request_field_limit():
    # Worst-case UTF-8 bytes for both accepted request fields still fit.
    assert _prompt_byte_budget(None, {}) > (32_000 + 8_000) * 4


def test_history_exact_boundary_keeps_newest_complete_turn_and_system_prompt():
    messages = [
        {"role": "system", "content": "safety"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new answer"},
        {"role": "user", "content": "current"},
    ]
    fixed = _message_budget_bytes(messages[0]) + _message_budget_bytes(messages[-1])
    newest_turn = _message_budget_bytes(messages[3]) + _message_budget_bytes(messages[4])

    bounded, dropped, _ = _bound_payload_history(
        messages, prompt_budget_bytes=fixed + newest_turn
    )

    assert bounded == [messages[0], messages[3], messages[4], messages[5]]
    assert dropped == 2


def test_history_one_byte_below_boundary_drops_whole_turn_not_just_its_user():
    messages = [
        {"role": "system", "content": "safety"},
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new answer"},
        {"role": "user", "content": "current"},
    ]
    exact = sum(_message_budget_bytes(message) for message in messages)

    bounded, dropped, _ = _bound_payload_history(
        messages, prompt_budget_bytes=exact - 1
    )

    assert bounded == [messages[0], messages[-1]]
    assert dropped == 2


def test_history_byte_accounting_is_not_bypassed_by_multibyte_input():
    message = {"role": "user", "content": "🙂"}
    assert _message_budget_bytes(message) == 4 + MESSAGE_ENVELOPE_RESERVE_BYTES


def test_history_refuses_fixed_system_and_current_content_over_budget():
    messages = [
        {"role": "system", "content": "safety"},
        {"role": "user", "content": "current"},
    ]
    fixed = sum(_message_budget_bytes(message) for message in messages)
    with pytest.raises(ValueError, match="fixed prompt"):
        _bound_payload_history(messages, prompt_budget_bytes=fixed - 1)


# --- reasoning_effort is validated server-side ------------------------------
#
# The UI only offers valid values, so these guard the paths the UI cannot: a
# direct API caller, and a value left stale in session params after the user
# switches to a model with a different (or empty) allowed set. Forwarding an
# invalid value produces an opaque mid-stream 400 from Foundry.


def test_effective_params_keeps_a_valid_reasoning_effort():
    entry = load_catalog().get("gpt-5.6-sol")
    assert entry is not None
    out = _effective_params({"reasoning_effort": "xhigh"}, entry)
    assert out["reasoning_effort"] == "xhigh"


def test_effective_params_drops_minimal_for_gpt56():
    """The value the previous name-based rule would have offered this model.

    gpt-5.6 400s on "minimal" while gpt-5.4 accepts it, so this is the case
    where a wrong allowed-set reaches the user as an opaque provider error.
    """
    catalog = load_catalog()
    entry = catalog.get("gpt-5.6-sol")
    assert entry is not None
    assert "minimal" not in entry.reasoningEffortOptions
    assert "reasoning_effort" not in _effective_params({"reasoning_effort": "minimal"}, entry)

    # ... and is NOT dropped for a model that does accept it.
    older = catalog.get("gpt-5.4")
    assert older is not None
    assert _effective_params({"reasoning_effort": "minimal"}, older)["reasoning_effort"] == (
        "minimal"
    )


def test_effective_params_drops_minimal_for_o_series():
    # "minimal" is a GPT-5-only value; o-series deployments 400 on it.
    entry = load_catalog().get("o3")
    assert entry is not None
    assert "minimal" not in entry.reasoningEffortOptions
    out = _effective_params({"reasoning_effort": "minimal"}, entry)
    assert "reasoning_effort" not in out
    # a value o3 does accept survives
    assert _effective_params({"reasoning_effort": "high"}, entry)["reasoning_effort"] == "high"


def test_effective_params_narrows_to_the_single_value_gpt5_pro_takes():
    entry = load_catalog().get("gpt-5-pro")
    assert entry is not None
    for rejected in ("none", "minimal", "low", "medium", "xhigh"):
        assert "reasoning_effort" not in _effective_params(
            {"reasoning_effort": rejected}, entry
        ), rejected
    assert _effective_params({"reasoning_effort": "high"}, entry)["reasoning_effort"] == "high"


def test_effective_params_drops_reasoning_effort_for_non_reasoning_models():
    entry = load_catalog().get("Mistral-Large-3")
    assert entry is not None
    out = _effective_params({"reasoning_effort": "high"}, entry)
    assert "reasoning_effort" not in out


def test_effective_params_drops_reasoning_effort_for_metadata_less_models():
    # model-router returns early from the max_tokens branch; the effort check
    # must still run, which it would not if it lived after that early return.
    entry = load_catalog().get("model-router")
    assert entry is not None
    assert entry.maxOutputTokens is None
    out = _effective_params({"reasoning_effort": "high"}, entry)
    assert "reasoning_effort" not in out
    assert "reasoning_effort" not in _effective_params({"reasoning_effort": "high"}, None)


def test_effective_params_drops_a_nonsense_reasoning_effort():
    entry = load_catalog().get("gpt-5.6-sol")
    assert entry is not None
    for bogus in ("ultra", "HIGH", "", 3, True):
        out = _effective_params({"reasoning_effort": bogus}, entry)
        assert "reasoning_effort" not in out, bogus


def test_effective_params_leaves_absent_reasoning_effort_absent():
    entry = load_catalog().get("gpt-5.6-sol")
    assert entry is not None
    out = _effective_params({"max_tokens": 100}, entry)
    assert "reasoning_effort" not in out
