"""Audit P1-2: Code Interpreter entitlement + usage accounting.

The finding was that the library compute path called Foundry **directly** (the
documented exception to the SimpleL7Proxy -> APIM -> Foundry rule, because a
stateful Azure-managed sandbox is not a routable chat-completions deployment)
with no entitlement check and no ledger row, for up to three sandbox executions
per tool turn — each of which can upload, run and delete provider resources.

These tests cover the four decisions taken on that finding:

1. The unit of consumption is a dedicated ``computeExecutionsPerDay`` limit, not
   tokens (a sandbox is billed per session) and not requests-per-minute (a
   30-second sandbox is not a trivial chat turn).
2. The gate runs before **each** provider call, not once per turn — so a limit
   can be reached mid-turn, and that must come back as a structured tool result
   the model can explain, never an exception that kills the turn.
3. The charge lands on **attempt**, not success: a sandbox that spun up and then
   failed still cost money and still created provider resources.
4. The ledger row carries a **distinct target identity**, so direct compute
   cannot hide inside the parent chat charge in the admin rollups.

Wherever the assertion is about enforcement rather than plumbing, the REAL
:class:`EntitlementService` and :class:`UsageService` are used over an in-memory
ledger — a fake that answers "denied" on a counter rather than on actual ledger
state cannot catch the bug it exists to catch.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ai4ia_api.code_interpreter.client import CodeInterpreterError
from ai4ia_api.code_interpreter.models import CodeInterpreterResult
from ai4ia_api.entitlements.memory_store import InMemoryEntitlementStore
from ai4ia_api.entitlements.models import Entitlement, EntitlementLimits
from ai4ia_api.entitlements.service import EntitlementService
from ai4ia_api.library.blob_store import InMemoryBlobStore
from ai4ia_api.library.compute_capability import (
    MAX_RUNS_PER_TURN,
    RUN_CODE_TOOL_NAME,
    build_compute_capability,
)
from ai4ia_api.library.export import DocumentExportService
from ai4ia_api.library.memory_repo import InMemoryDocumentLibraryRepository
from ai4ia_api.library.models import DocumentStatus, UserDocument
from ai4ia_api.library.blob_store import PARSED_NAME, blob_path
from ai4ia_api.library.retrieval import DocumentRetrievalService
from ai4ia_api.usage.memory_repo import InMemoryUsageRepository
from ai4ia_api.usage.models import (
    CODE_INTERPRETER_MODEL,
    CODE_INTERPRETER_PROVIDER,
    CODE_INTERPRETER_TARGET,
    TokenUsage,
    UsageRecord,
    UsageTarget,
    WindowTotals,
    summarize_records,
)
from ai4ia_api.usage.pricing import PricingBook
from ai4ia_api.usage.service import UsageService
from tests.conftest import make_settings


class FakeCI:
    """Records runs; can be scripted to fail."""

    def __init__(self, result=None, raise_exc=None):
        self.result = result or CodeInterpreterResult(status="completed", output_text="42")
        self.raise_exc = raise_exc
        self.calls: list[dict] = []
        self.uploads: list[dict] = []
        self.deletes: list[str] = []
        self.upload_file_id = "file-abc"
        self.upload_raise = None

    async def run(self, *, instructions, user_input, file_ids=None):
        self.calls.append({"file_ids": file_ids})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result

    async def upload_file(self, *, filename, content, content_type=None):
        self.uploads.append({"filename": filename})
        if self.upload_raise is not None:
            raise self.upload_raise
        return self.upload_file_id

    async def delete_file(self, file_id):
        self.deletes.append(file_id)
        return True

    async def close(self):
        return None


class CountingReader:
    """A UsageWindowReader that records every read, so a test can assert the
    unlimited hot path stays free of ledger IO."""

    def __init__(self, totals: WindowTotals | None = None) -> None:
        self.totals = totals or WindowTotals()
        self.calls = 0

    async def window_totals(self, user_id, *, since, now=None):
        self.calls += 1
        return self.totals


class BoomReader:
    async def window_totals(self, user_id, *, since, now=None):
        raise RuntimeError("ledger down")


def _settings(**overrides):
    base = dict(document_understanding_enabled=True, document_compute_enabled=True)
    base.update(overrides)
    return make_settings(**base)


async def _seed_doc(library, blob, *, user="u1", parsed="name,amount\nA,10\n"):
    doc = UserDocument(
        userId=user, filename="data.csv", status=DocumentStatus.ready, summary="seed"
    )
    path = blob_path(user, doc.id, PARSED_NAME)
    await blob.put(path, parsed.encode("utf-8"), "text/markdown")
    doc.parsedPath = path
    await library.create_document(doc)
    return doc


def _real_services(*, limits: EntitlementLimits | None = None, user="u1"):
    """The REAL entitlement + usage services over an in-memory ledger.

    Enforcement is decided from actual ledger rows, so a test that records
    executions and then expects a denial is exercising the same code path
    production does, not a counter in a fake.
    """
    usage = UsageService(InMemoryUsageRepository(), PricingBook({}, currency="USD", version=None), enabled=True)
    store = InMemoryEntitlementStore()
    ents = EntitlementService(
        store,
        usage,
        Entitlement.unlimited("__default__"),
        enabled=True,
        # No cache, so a row recorded between two checks is visible immediately.
        cache_ttl_seconds=0,
    )
    return usage, ents, store


async def _apply(store, user, limits: EntitlementLimits):
    await store.put(Entitlement(id=user, userId=user, **limits.model_dump()))


def _caps(library, blob, ci, settings, *, ents, usage, user="u1", session="s1"):
    return build_compute_capability(
        retrieval=DocumentRetrievalService(
            library=library, blob_store=blob, chunk_store=None, embedder=None,
            settings=settings,
        ),
        code_interpreter=ci,
        export=DocumentExportService(library=library, blob_store=blob, settings=settings),
        entitlements=ents,
        metering=usage,
        settings=settings,
        user_id=user,
        session_id=session,
        nonce="nn",
    )[1]


# --------------------------------------------------------------------------
# Decision 1: the limit is its own axis, and it is a real limit
# --------------------------------------------------------------------------
def test_compute_limit_makes_a_user_not_unlimited():
    """A user whose ONLY limit is the sandbox cap must not take the unlimited
    fast path — otherwise the cap could never be reached."""
    assert EntitlementLimits().is_unlimited is True
    limited = EntitlementLimits(computeExecutionsPerDay=5)
    assert limited.has_any_limit is True
    assert limited.is_unlimited is False


def test_compute_limit_rejects_negative_and_accepts_zero_hard_block():
    assert EntitlementLimits(computeExecutionsPerDay=0).computeExecutionsPerDay == 0
    with pytest.raises(ValueError):
        EntitlementLimits(computeExecutionsPerDay=-1)


def test_window_totals_counts_only_code_interpreter_rows():
    """The rolling counter must key off the DISTINCT provider identity, not the
    request count — otherwise a chat turn would spend the sandbox allowance."""
    now = datetime.now(timezone.utc)
    records = [
        UsageRecord(userId="u", sessionId="s", model="gpt-4.1", provider="azure_openai"),
        UsageRecord(
            userId="u", sessionId="s", model=CODE_INTERPRETER_MODEL,
            provider=CODE_INTERPRETER_PROVIDER,
        ),
        UsageRecord(
            userId="u", sessionId="s", model=CODE_INTERPRETER_MODEL,
            provider=CODE_INTERPRETER_PROVIDER, status="error",
        ),
    ]
    summary = summarize_records(
        "u", records, since_days=1, from_time=now - timedelta(days=1), to_time=now
    )
    assert summary.totalRequests == 3
    # Two sandbox executions, one of which errored — an errored attempt still
    # burned a container, so it still counts.
    assert summary.computeExecutions == 2


# --------------------------------------------------------------------------
# Decision 1 + non-vacuity: allowed under the limit, denied at it
# --------------------------------------------------------------------------
async def test_compute_scope_allows_under_limit_and_denies_at_it():
    """The identical call, same user, same service, same ledger: allowed while
    under the cap and denied once the cap is reached. Asserting only the denial
    would prove nothing."""
    usage, ents, store = _real_services()
    await _apply(store, "u1", EntitlementLimits(computeExecutionsPerDay=2))

    assert (await ents.check("u1", scope="compute")).allowed is True

    for _ in range(2):
        await usage.record_completion(
            user_id="u1", session_id="s1", model_id=CODE_INTERPRETER_MODEL,
            target=UsageTarget.code_interpreter("gpt-4.1"),
            usage=TokenUsage(known=False, complete=False, calls=1),
        )

    denied = await ents.check("u1", scope="compute")
    assert denied.allowed is False
    assert denied.code == 429
    assert denied.limit_kind == "compute_executions_per_day"
    assert denied.limit == 2 and denied.used == 2
    assert denied.retry_after_seconds == 24 * 60 * 60


async def test_chat_turns_do_not_spend_the_sandbox_allowance():
    """Two chat turns then a compute check: the compute allowance is untouched,
    because chat rows carry the ordinary provider."""
    usage, ents, store = _real_services()
    await _apply(store, "u1", EntitlementLimits(computeExecutionsPerDay=2))
    for _ in range(5):
        await usage.record_completion(
            user_id="u1", session_id="s1", model_id="gpt-4.1",
            target=UsageTarget(provider="azure_openai", deployment="gpt-4.1"),
            usage=TokenUsage(prompt=1, completion=1, total=2, known=True, calls=1),
        )
    assert (await ents.check("u1", scope="compute")).allowed is True


async def test_exhausted_sandbox_allowance_never_blocks_chat():
    """A scoped limit stays scoped: burning the sandbox cap must not lock the
    user out of ordinary conversation."""
    usage, ents, store = _real_services()
    await _apply(store, "u1", EntitlementLimits(computeExecutionsPerDay=1))
    await usage.record_completion(
        user_id="u1", session_id="s1", model_id=CODE_INTERPRETER_MODEL,
        target=UsageTarget.code_interpreter(None),
        usage=TokenUsage(known=False, complete=False, calls=1),
    )
    assert (await ents.check("u1", scope="compute")).allowed is False
    assert (await ents.check("u1", scope="chat")).allowed is True


# --------------------------------------------------------------------------
# Hot path: unlimited is still free
# --------------------------------------------------------------------------
async def test_unlimited_user_does_zero_ledger_io_on_both_scopes():
    reader = CountingReader()
    svc = EntitlementService(
        InMemoryEntitlementStore(), reader, Entitlement.unlimited("__default__")
    )
    assert (await svc.check("alice")).allowed is True
    assert (await svc.check("alice", scope="compute")).allowed is True
    assert reader.calls == 0


async def test_compute_only_limit_costs_a_chat_turn_zero_ledger_io():
    """Adding a sandbox cap must not silently start charging a daily ledger read
    to every chat turn — the daily read is skipped when no daily limit applies to
    the scope being checked."""
    store = InMemoryEntitlementStore()
    await store.put(Entitlement(id="u", userId="u", computeExecutionsPerDay=5))
    reader = CountingReader()
    svc = EntitlementService(store, reader, Entitlement.unlimited("__default__"))

    assert (await svc.check("u", scope="chat")).allowed is True
    assert reader.calls == 0  # the whole point

    assert (await svc.check("u", scope="compute")).allowed is True
    assert reader.calls == 1  # ...but compute does read, or it could not enforce


# --------------------------------------------------------------------------
# Failure semantics: fail open, except disabled
# --------------------------------------------------------------------------
async def test_ledger_error_fails_open_for_compute():
    store = InMemoryEntitlementStore()
    await store.put(Entitlement(id="u", userId="u", computeExecutionsPerDay=1))
    svc = EntitlementService(store, BoomReader(), Entitlement.unlimited("__default__"))
    decision = await svc.check("u", scope="compute")
    assert decision.allowed is True  # availability over strict caps


async def test_disabled_account_still_denies_compute():
    store = InMemoryEntitlementStore()
    await store.put(Entitlement(id="u", userId="u", disabled=True))
    reader = CountingReader()
    svc = EntitlementService(store, reader, Entitlement.unlimited("__default__"))
    decision = await svc.check("u", scope="compute")
    assert decision.allowed is False
    assert decision.code == 403
    assert decision.limit_kind == "disabled"
    assert reader.calls == 0  # disabled short-circuits before any ledger read


def test_startup_guard_covers_the_compute_limit_too():
    """Server-authoritative enforcement: a positive limit can never silently
    no-op for lack of a ledger. Sandbox executions accrue in the same ledger as
    tokens and cost, so the existing guard covers the new limit — this test is
    what keeps that true if the guard is ever narrowed."""
    settings = make_settings(entitlements_enabled=True, usage_metering_enabled=False)
    with pytest.raises(RuntimeError, match="usage metering"):
        settings.validate_runtime()


# --------------------------------------------------------------------------
# Decisions 2/3/4 at the tool seam
# --------------------------------------------------------------------------
async def test_run_code_is_gated_before_any_provider_io():
    """The gate must run before the raw-file UPLOAD, not merely before the run —
    an upload is already a call to Foundry."""
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    ci = FakeCI()
    usage, ents, store = _real_services()
    await _apply(store, "u1", EntitlementLimits(disabled=True))
    settings = _settings(code_interpreter_raw_files_enabled=True)
    handlers = _caps(library, blob, ci, settings, ents=ents, usage=usage)
    doc = await _seed_doc(library, blob)

    res = await handlers[RUN_CODE_TOOL_NAME](
        {"document_id": doc.id, "task": "sum"}, ctx=None
    )
    assert res["status"] == "denied"
    assert "result" not in res
    assert ci.uploads == [] and ci.calls == []


async def test_limit_reached_mid_turn_returns_a_structured_tool_result():
    """A turn may perform up to MAX_RUNS_PER_TURN executions, so one user message
    can legitimately exhaust the allowance part-way through. The denial must be a
    tool result the model can explain, not an exception that kills the turn."""
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    ci = FakeCI()
    usage, ents, store = _real_services()
    await _apply(store, "u1", EntitlementLimits(computeExecutionsPerDay=1))
    handlers = _caps(library, blob, ci, _settings(), ents=ents, usage=usage)
    doc = await _seed_doc(library, blob)

    first = await handlers[RUN_CODE_TOOL_NAME](
        {"document_id": doc.id, "task": "sum"}, ctx=None
    )
    assert "error" not in first  # under the cap: really ran

    second = await handlers[RUN_CODE_TOOL_NAME](
        {"document_id": doc.id, "task": "sum again"}, ctx=None
    )
    # A dict, not a raised exception -> the tool loop keeps going and the model
    # can tell the user what happened.
    assert isinstance(second, dict)
    assert second["status"] == "denied"
    assert "budget" in second["error"].lower() or "reached" in second["error"].lower()
    assert second["retry_after_seconds"] == 24 * 60 * 60
    assert len(ci.calls) == 1  # the second execution never reached the provider
    assert MAX_RUNS_PER_TURN > 1  # ...and it was NOT the per-turn budget that stopped it


async def test_every_execution_attempt_is_metered_with_the_distinct_target():
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    ci = FakeCI()
    usage, ents, _ = _real_services()
    settings = _settings(code_interpreter_model="gpt-4.1")
    handlers = _caps(library, blob, ci, settings, ents=ents, usage=usage)
    doc = await _seed_doc(library, blob)

    await handlers[RUN_CODE_TOOL_NAME]({"document_id": doc.id, "task": "sum"}, ctx=None)

    summary = await usage.summarize("u1")
    assert summary.totalRequests == 1
    assert summary.computeExecutions == 1
    rec = (await usage.summarize_session("u1", "s1")).latest
    assert rec is not None
    # Decision 4: direct compute cannot hide inside the parent chat charge.
    assert rec.provider == CODE_INTERPRETER_PROVIDER
    assert rec.target == CODE_INTERPRETER_TARGET
    assert rec.agent == CODE_INTERPRETER_TARGET
    assert rec.model == CODE_INTERPRETER_MODEL
    assert rec.deployment == "gpt-4.1"  # still traceable to the serving deployment
    # Honesty model: a sandbox reports no tokens, so usage is UNKNOWN, and an
    # unknown cost is never rendered as zero.
    assert rec.usageKnown is False
    assert rec.billable is False
    assert rec.costKnown is False
    assert rec.estCostMicroUsd is None


async def test_failed_execution_is_still_metered_with_an_error_status():
    """Decision 3: charge on attempt. A sandbox that spun up and then failed
    still cost money and still created provider resources."""
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    ci = FakeCI(raise_exc=CodeInterpreterError(500, "boom"))
    usage, ents, _ = _real_services()
    handlers = _caps(library, blob, ci, _settings(), ents=ents, usage=usage)
    doc = await _seed_doc(library, blob)

    res = await handlers[RUN_CODE_TOOL_NAME](
        {"document_id": doc.id, "task": "sum"}, ctx=None
    )
    assert "error" in res and "result" not in res

    summary = await usage.summarize("u1")
    assert summary.computeExecutions == 1
    assert summary.erroredRequests == 1
    rec = (await usage.summarize_session("u1", "s1")).latest
    assert rec is not None and rec.status == "error"
    assert rec.provider == CODE_INTERPRETER_PROVIDER


async def test_failed_execution_counts_against_the_next_check():
    """The consequence of decision 3 that actually matters: a user cannot burn
    the provider for free by triggering failures."""
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    ci = FakeCI(raise_exc=CodeInterpreterError(500, "boom"))
    usage, ents, store = _real_services()
    await _apply(store, "u1", EntitlementLimits(computeExecutionsPerDay=1))
    handlers = _caps(library, blob, ci, _settings(), ents=ents, usage=usage)
    doc = await _seed_doc(library, blob)

    await handlers[RUN_CODE_TOOL_NAME]({"document_id": doc.id, "task": "sum"}, ctx=None)
    second = await handlers[RUN_CODE_TOOL_NAME](
        {"document_id": doc.id, "task": "sum"}, ctx=None
    )
    assert second["status"] == "denied"
    assert len(ci.calls) == 1


async def test_nothing_is_metered_when_the_call_never_reaches_the_provider():
    """A rejected document (not ready / not owned / not selected) creates no
    sandbox, so it must not consume the allowance."""
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    ci = FakeCI()
    usage, ents, _ = _real_services()
    handlers = _caps(library, blob, ci, _settings(), ents=ents, usage=usage)

    res = await handlers[RUN_CODE_TOOL_NAME](
        {"document_id": "does-not-exist", "task": "sum"}, ctx=None
    )
    assert "error" in res
    assert ci.calls == []
    assert (await usage.summarize("u1")).computeExecutions == 0


async def test_ledger_outage_does_not_deny_a_user_their_compute():
    """Fail open, except disabled — end to end at the tool seam."""
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    ci = FakeCI()
    store = InMemoryEntitlementStore()
    await store.put(Entitlement(id="u1", userId="u1", computeExecutionsPerDay=0))
    broken = EntitlementService(
        store, BoomReader(), Entitlement.unlimited("__default__"), cache_ttl_seconds=0
    )
    usage = UsageService(InMemoryUsageRepository(), PricingBook({}, currency="USD", version=None), enabled=True)
    handlers = _caps(library, blob, ci, _settings(), ents=broken, usage=usage)
    doc = await _seed_doc(library, blob)

    res = await handlers[RUN_CODE_TOOL_NAME](
        {"document_id": doc.id, "task": "sum"}, ctx=None
    )
    assert "error" not in res
    assert len(ci.calls) == 1


async def test_hard_block_of_zero_denies_the_first_execution():
    library, blob = InMemoryDocumentLibraryRepository(), InMemoryBlobStore()
    ci = FakeCI()
    usage, ents, store = _real_services()
    await _apply(store, "u1", EntitlementLimits(computeExecutionsPerDay=0))
    handlers = _caps(library, blob, ci, _settings(), ents=ents, usage=usage)
    doc = await _seed_doc(library, blob)

    res = await handlers[RUN_CODE_TOOL_NAME](
        {"document_id": doc.id, "task": "sum"}, ctx=None
    )
    assert res["status"] == "denied"
    assert ci.calls == []
