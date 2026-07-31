"""Per-model metadata + scaling.

Covers: catalog metadata round-trips catalog -> API -> serialization; a model
without metadata falls back to the fixed constants; the request max-output is
capped per model (lower-only) and still composes with the gateway's reasoning
param translation; the document budget scales from the context window.
"""
from __future__ import annotations

import json
from pathlib import Path

from ai4ia_api.catalog import DeploymentOption, ModelEntry, load_catalog
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.routers.chat import (
    DOC_CONTEXT_BUDGET,
    DOC_CONTEXT_BUDGET_MAX,
    GLOBAL_DEFAULT_MAX_TOKENS,
    _doc_budget_for,
    _effective_params,
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
