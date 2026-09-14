"""Actual HTTP financial shapes consumed by the web's strict money validators."""
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from tests.test_gateway_attempts import wire as wire
from tests.test_workflow_monetary_dispatch import monetary as monetary, start
from tests.test_workflow_automation_service import install

CONTRACT = json.loads((
    Path(__file__).resolve().parents[2] / "web" / "test-fixtures" / "workflow_money.json"
).read_text(encoding="utf-8"))


def stable(value):
    if isinstance(value, dict):
        result = {key: stable(child) for key, child in value.items()}
        for key in ("budgetId", "quoteDigest", "bindingDigest"):
            if isinstance(result.get(key), str):
                result[key] = "a" * 64
        if "quotedAt" in result:
            result["quotedAt"] = "2099-01-01T00:00:00Z"
        if "expiresAt" in result:
            result["expiresAt"] = "2099-01-01T00:10:00Z"
        return result
    return value


def test_actual_uncapped_start_read_list_and_review_keep_financial_discriminators(client):
    service, _, _ = install(client, gated=True)
    assert client.post("/api/workflows", json={
        "name": "response-contract", "steps": [{"agent": "testleaf", "instruction": "{input}"}],
    }).status_code == 201
    response = client.post("/api/workflows/automation/runs", json={
        "selection": {"name": "response-contract", "model": "gpt-5.4"},
        "input": "Send the report.", "limits": {"spendMode": "no_hard_dollar_cap"},
        "idempotencyKey": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z") + "~" + uuid4().hex,
    })
    assert response.status_code == 202, response.text
    row = response.json()
    assert row["budget"] == CONTRACT["uncappedBudget"]
    owner, run_id = row["runId"].partition(":")[0], row["runId"]
    assert client.portal.call(service.advance, owner, run_id)["status"] == "awaiting_approval"
    for path in ("/approvals", "/runs", f"/runs/{run_id}"):
        body = client.get("/api/workflows/automation" + path).json()
        view = body["runs"][0] if "runs" in body else body
        assert view["budget"] == CONTRACT["uncappedBudget"]
        assert stable(view["approval"]["spend"]) == CONTRACT["quotedSpend"]
    state, message = client.portal.call(service.load, owner, run_id)
    review_path = f"/api/workflows/automation/runs/{run_id}/approvals/{state.draft.id}/review"
    reviewed = client.post(review_path)
    assert reviewed.status_code == 200, reviewed.text
    assert stable(reviewed.json()["spendEvidence"]) == CONTRACT["quotedSpend"]
    assert stable(reviewed.json()["approval"]["spend"]) == CONTRACT["quotedSpend"]

    # A pre-monetary v3 record has neither optional spend field. Preserve that
    # source shape, but its HTTP projection still explicitly says unknown/USD.
    state, message = client.portal.call(service.load, owner, run_id)
    legacy = state.model_copy(deep=True)
    legacy.draft.challengeSpendDigest = None
    legacy.draft.spend = None
    legacy.draft.challenge = None
    client.portal.call(service.commit, state, message, legacy)
    restored = client.get(f"/api/workflows/automation/runs/{run_id}").json()
    assert restored["approval"]["spend"] == CONTRACT["legacySpend"]
    assert client.post(review_path).json()["spendEvidence"] == CONTRACT["legacySpend"]


def test_actual_capped_start_and_read_use_the_same_complete_consumer_contract(client, monetary):
    _, _, body = start(client)
    accepted = client.post("/api/workflows/automation/runs", json=body)
    assert accepted.status_code == 202, accepted.text
    view = accepted.json()
    assert stable(view["budget"]) == CONTRACT["cappedBudget"]
    read = client.get(f"/api/workflows/automation/runs/{view['runId']}").json()
    assert stable(read["budget"]) == CONTRACT["cappedBudget"]
