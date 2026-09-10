"""Real conditional batch shapes and disjoint owner-partition records."""
from __future__ import annotations

import copy

import pytest
from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosResourceNotFoundError

from ai4ia_api.agents.cosmos_store import CosmosUserAgentStore
from ai4ia_api.publishing.store import CosmosRecordStore
from ai4ia_api.workflows.cosmos_store import CosmosWorkflowStore
from ai4ia_api.workflows.record_types import (
    AGENT_DEFINITION_KIND, AUTOMATION_OWNER_ID, AUTOMATION_OWNER_KIND,
    CONTROL_RECORD_PREFIX, WORKFLOW_DEFINITION_KIND,
)


class ConditionalContainer:
    def __init__(self):
        self.items = {
            "source": {"id": "source", "userId": "owner", "revision": 1, "_etag": "source-1"},
            "head": {"id": "head", "userId": "owner", "revision": 1, "_etag": "head-1"},
        }
        self.race = False
        self.observed_etags = []

    async def read_item(self, *, item, partition_key):
        value = self.items.get(item)
        if value is None or value["userId"] != partition_key:
            raise CosmosResourceNotFoundError(message="missing")
        return copy.deepcopy(value)

    async def read(self):
        return {"id": "existing-container"}

    async def execute_item_batch(self, *, batch_operations, partition_key):
        if self.race:
            self.race = False
            self.items["source"].update({"revision": 2, "_etag": "edited"})
        pending = copy.deepcopy(self.items)
        for index, operation in enumerate(batch_operations):
            verb, args, *extras = operation
            options = extras[0] if extras else {}
            identifier = args[0]["id"] if verb == "create" else args[0]
            current = pending.get(identifier)
            # The SDK consumes conditional options during serialization. A retry
            # must receive fresh dictionaries, not the mutated prior attempt.
            etag = options.pop("if_match_etag", None)
            self.observed_etags.append((identifier, etag))
            failure = (
                409 if verb == "create" and current is not None else
                404 if verb != "create" and current is None else
                412 if verb != "create" and etag is not None and current["_etag"] != etag else
                None
            )
            if failure is not None:
                raise CosmosBatchOperationError(
                    error_index=index, status_code=failure, headers={}, message="conflict",
                    operation_responses=[{"statusCode": failure}],
                )
            if verb == "delete":
                pending.pop(identifier)
            else:
                body = args[0] if verb == "create" else args[1]
                assert body["userId"] == partition_key
                pending[identifier] = {**copy.deepcopy(body), "_etag": f"written-{identifier}"}
        self.items = pending


@pytest.mark.parametrize("race", [False, True])
async def test_source_and_head_etags_fence_review_and_retry_options_are_fresh(race):
    container = ConditionalContainer()
    records = CosmosRecordStore(container)
    source = await records.read("owner", "source")
    head = await records.read("owner", "head")
    expected = {"source": source, "head": head, "decision": None}
    changes = {
        "head": {"id": "head", "userId": "owner", "revision": 2},
        "decision": {"id": "decision", "userId": "owner", "decision": "approved"},
    }
    container.race = race
    applied = await records.atomic("owner", expected, changes)
    assert applied is not race
    if race:
        assert "decision" not in container.items
        assert container.items["head"]["revision"] == 1
        assert not await records.atomic("owner", expected, changes)
        assert container.observed_etags == [("source", "source-1"), ("source", "source-1")]
    else:
        assert container.items["decision"]["decision"] == "approved"
        assert ("source", "source-1") in container.observed_etags
        assert ("head", "head-1") in container.observed_etags
    assert source.etag == "source-1"


@pytest.mark.parametrize("store_type,kind,source", [
    (CosmosUserAgentStore, AGENT_DEFINITION_KIND, {"systemPrompt": "Owned source."}),
    (CosmosWorkflowStore, WORKFLOW_DEFINITION_KIND, {"steps": []}),
])
async def test_definition_queries_keep_legacy_and_exclude_every_control_kind(store_type, kind, source):
    legacy = {"id": "legacy", "name": "legacy", "userId": "owner", "displayName": "Legacy", **source}
    rows = [
        legacy, {**legacy, "id": "modern", "name": "modern", "recordKind": kind},
        {**legacy, "id": AUTOMATION_OWNER_ID, "name": AUTOMATION_OWNER_ID},
        {**legacy, "recordKind": AUTOMATION_OWNER_KIND},
        {**legacy, "recordKind": "unknown-kind"},
        {**legacy, "userId": "other"},
    ]

    class Container:
        def query_items(self, *, query, parameters, partition_key):
            assert "NOT STARTSWITH(c.id, @controlPrefix)" in query
            assert "NOT IS_DEFINED(c.recordKind)" in query
            assert "c.recordKind = @definitionKind" in query
            assert partition_key == "owner"
            assert {"name": "@controlPrefix", "value": CONTROL_RECORD_PREFIX} in parameters
            assert {"name": "@definitionKind", "value": kind} in parameters

            async def items():
                for row in rows:
                    yield copy.deepcopy(row)
            return items()

    store = object.__new__(store_type)
    store._container = Container()
    values = await store.list("owner")
    assert [value.name for value in values] == ["legacy", "modern"]


async def test_conditional_record_store_rejects_cross_owner_write():
    records = CosmosRecordStore(ConditionalContainer())
    before = await records.read("owner", "source")
    with pytest.raises(ValueError, match="ownership"):
        await records.atomic(
            "owner", {"source": before},
            {"source": {"id": "source", "userId": "other"}},
        )


def test_published_source_evidence_fits_escaped_receipt_budget():
    import json

    from ai4ia_api.publishing.refs import AssetVersionRef, PublicationEvidence
    from ai4ia_api.receipts import (
        MAX_RECEIPT_BYTES, ReceiptRuntime, ReceiptToolCall, build_receipt, json_payload,
    )

    source = PublicationEvidence(
        source=AssetVersionRef(
            kind="workflow", ownerId="owner", assetId="a" * 32, version=20, digest="b" * 64,
        ),
        approvedProfileDigest="c" * 64, effectiveSubsetDigest="d" * 64,
        approvalDigest="e" * 64, mode="workflow", scope="step:5",
        narrowing=("empty_document_scope",), exclusions=("skills_excluded_by_author",),
    )
    receipt = build_receipt(
        runtime=ReceiptRuntime(publication=source),
        prompt_messages=[{"role": "user", "content": "\u6f22" * 10000}] * 40,
        calls=[ReceiptToolCall(
            tool="calculator", outcome="result",
            arguments=json_payload({"input": "\u6f22" * 10000}),
            result=json_payload({"output": "\u6f22" * 10000}),
        )] * 16,
    )
    encoded = json.dumps(receipt.model_dump(mode="json"), ensure_ascii=True).encode("ascii")
    assert len(encoded) <= MAX_RECEIPT_BYTES
    assert receipt.runtime.publication == source
    assert receipt.truncated
