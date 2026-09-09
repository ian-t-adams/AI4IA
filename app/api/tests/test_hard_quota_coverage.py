"""New gateway write surfaces cannot silently avoid the common admission seam."""
from __future__ import annotations

import ast
from pathlib import Path

from ai4ia_api.hard_quota.coverage import COVERAGE, AttemptEnvelope, reservation_bounds
from ai4ia_api.hard_quota.models import Amounts, Bounds
from ai4ia_api.usage.pricing import PriceRate, PricingBook, conservative_token_cost
from tests.test_hard_quota_dispatch import DEPLOYMENT, Harness

ROOT = Path(__file__).resolve().parents[1] / "src" / "ai4ia_api"


def test_every_gateway_post_and_stream_uses_the_guarded_transport_seams():
    tree = ast.parse((ROOT / "gateway" / "client.py").read_text(encoding="utf-8"))
    client = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ModelGatewayClient")
    sites = set()
    for method in client.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(method):
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "client"
                and node.func.attr in {"post", "put", "patch", "request", "send", "stream"}
            ):
                sites.add((method.name, node.func.attr))
    assert sites == {("_post", "post"), ("_stream_request", "stream")}
    for name in ("_post", "_stream_request"):
        method = next(method for method in client.body if getattr(method, "name", None) == name)
        assert any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "admitted_dispatch" for node in ast.walk(method)
        )


def test_meter_coverage_is_explicit_and_shipping_gateway_has_no_attempt_envelope():
    h = Harness()
    assert set(COVERAGE) == {
        "chat", "embedding", "image", "video", "transcription", "speech",
        "realtime", "compute", "document", "web_search", "mcp", "external_tool",
    }
    body = {"messages": [{"role": "user", "content": "text"}]}
    unknown = reservation_bounds(
        "chat", body, deployment=DEPLOYMENT, catalog=h.catalog, pricing=h.pricing,
    )
    assert unknown.amounts.tokens is None and unknown.amounts.microUsd is None
    bounded = reservation_bounds(
        "chat", body, deployment=DEPLOYMENT, catalog=h.catalog, pricing=h.pricing,
        attempts=AttemptEnvelope("fixture-single-send-v1", 1),
    )
    assert bounded.amounts.tokens == 120 and bounded.amounts.microUsd == 140
    for key, value in (("n", 2), ("tools", [{"type": "web_search_preview"}]),
                       ("previous_response_id", "opaque")):
        unsupported = reservation_bounds(
            "chat", {**body, key: value}, deployment=DEPLOYMENT,
            catalog=h.catalog, pricing=h.pricing, attempts=AttemptEnvelope("fixture-v1", 1),
        )
        assert unsupported.amounts.tokens is None


def test_unpriced_fixture_cannot_become_a_free_dollar_bound():
    h = Harness()
    bounded = reservation_bounds(
        "chat", {"messages": []}, deployment=DEPLOYMENT, catalog=h.catalog,
        pricing=PricingBook({}, currency="USD", version=None),
        attempts=AttemptEnvelope("fixture-single-send-v1", 1),
    )
    assert bounded.amounts.tokens == 120
    assert bounded.amounts.microUsd is None


def test_shared_pricing_bound_is_versioned_and_rounds_conservatively():
    from pydantic import ValidationError
    import pytest

    book = PricingBook({"fixture": PriceRate(0.1, 0.2)}, currency="USD", version="v1")
    bounded = book.estimate_token_bound("fixture", prompt_tokens=1, completion_tokens=1)
    assert bounded.micro_usd == 1
    assert bounded.micro_usd >= book.estimate("fixture", prompt_tokens=1, completion_tokens=1).micro_usd
    assert bounded.version == "v1"
    for rate in ("NaN", "Infinity", "-1", "invalid"):
        assert conservative_token_cost(
            prompt_tokens=1, completion_tokens=1, input_rate=rate, output_rate="1",
        ) is None
    with pytest.raises(ValidationError, match="versioned"):
        Bounds(amounts=Amounts(microUsd=0), basis="request-v1")
