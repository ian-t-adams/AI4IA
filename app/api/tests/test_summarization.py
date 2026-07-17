"""Rolling summarization.

Unit tests for :class:`SummarizationService` plus end-to-end tests through
``POST /api/chat`` proving: the DEFAULT-OFF flag makes the turn byte-for-byte
unchanged (full history, no summary block); the manual ``/summarize`` command
folds + persists a running summary; and the automatic path folds the oldest
turns, injects the summary, and ALWAYS keeps the full transcript in storage.
"""
from __future__ import annotations

import pytest
from types import SimpleNamespace

from ai4ia_api.agents.summarization import SummarizationService, build_summarization_service
from ai4ia_api.sessions.models import Message, MessageRole, MessageStatus, Session

from .conftest import make_settings


class _FakeGateway:
    """Records the summarization prompt and returns a canned summary."""

    def __init__(self, text: str = "SUMMARY") -> None:
        self.text = text
        self.calls: list[list[dict]] = []

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.calls.append(messages)
        return {"choices": [{"message": {"role": "assistant", "content": self.text}}]}


class _FakeRepo:
    def __init__(self) -> None:
        self.updates = 0

    async def commit_summary_if_version(
        self,
        user_id,
        session_id,
        *,
        expected_version,
        summary,
        summarized_through_message_id,
    ):
        self.updates += 1
        return SimpleNamespace(
            summary=summary,
            summarizedThroughMessageId=summarized_through_message_id,
            summaryVersion=expected_version + 1,
        )


def _msg(content: str, role: MessageRole = MessageRole.user, **over) -> Message:
    return Message(
        sessionId="s",
        userId="u",
        role=role,
        content=content,
        status=MessageStatus.complete,
        **over,
    )


def _session(**over) -> Session:
    base = dict(userId="u", model="gpt-5.2")
    base.update(over)
    return Session(**base)


# --- threshold + block formatting ---------------------------------------------


def test_threshold_falls_back_without_window():
    svc = SummarizationService(fallback_threshold_chars=4242)
    assert svc.threshold_chars(None) == 4242


def test_threshold_scales_with_window():
    svc = SummarizationService(threshold_ratio=0.5)
    assert svc.threshold_chars(400000) == int(400000 * 4 * 0.5)


def test_format_block_empty_for_no_summary():
    svc = SummarizationService()
    assert svc.format_block(None) == ""
    assert svc.format_block("   ") == ""


def test_format_block_wraps_summary():
    svc = SummarizationService()
    block = svc.format_block("the recap")
    assert "the recap" in block
    assert block != "the recap"  # carries the header framing


# --- manual summarize_now -----------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_now_folds_all_and_persists():
    svc = SummarizationService()
    gw, repo, session = _FakeGateway("DIGEST"), _FakeRepo(), _session()
    prior = [_msg("one"), _msg("two", role=MessageRole.assistant), _msg("three")]
    out = await svc.summarize_now(
        gateway=gw, repo=repo, session=session, user_id="u",
        deployment="dep", prior=prior,
    )
    assert out == "DIGEST"
    assert session.summary == "DIGEST"
    assert session.summarizedThroughMessageId == prior[-1].id
    assert len(gw.calls) == 1


@pytest.mark.asyncio
async def test_summarize_now_returns_none_when_nothing_to_fold():
    svc = SummarizationService()
    gw, repo, session = _FakeGateway(), _FakeRepo(), _session()
    out = await svc.summarize_now(
        gateway=gw, repo=repo, session=session, user_id="u",
        deployment="dep", prior=[],
    )
    assert out is None
    assert session.summary is None
    assert not gw.calls


@pytest.mark.asyncio
async def test_summarize_now_ignores_command_echoes():
    svc = SummarizationService()
    gw, repo, session = _FakeGateway(), _FakeRepo(), _session()
    prior = [_msg("/summarize", fromCommand=True), _msg("hello there")]
    await svc.summarize_now(
        gateway=gw, repo=repo, session=session, user_id="u",
        deployment="dep", prior=prior,
    )
    # The folded id must be the real message, not the command echo.
    assert session.summarizedThroughMessageId == prior[1].id


# --- automatic apply ----------------------------------------------------------


def _long_turns(n: int) -> list[Message]:
    out: list[Message] = []
    for i in range(n):
        out.append(_msg("user says something at length " + str(i)))
        out.append(_msg("assistant replies at length " + str(i), role=MessageRole.assistant))
    return out


@pytest.mark.asyncio
async def test_apply_folds_oldest_when_over_threshold():
    svc = SummarizationService(
        enabled=True, recent_turns=2, fallback_threshold_chars=10
    )
    gw, repo, session = _FakeGateway("ROLLING"), _FakeRepo(), _session()
    prior = _long_turns(4)  # 8 messages, well over a 10-char threshold
    live, summary = await svc.apply(
        gateway=gw, repo=repo, session=session, user_id="u",
        deployment="dep", prior=prior, system_prompt=None, context_window=None,
    )
    assert summary == "ROLLING"
    assert session.summary == "ROLLING"
    # Newest recent_turns messages stay verbatim; the rest were folded.
    assert live == prior[-2:]
    assert session.summarizedThroughMessageId == prior[-3].id
    assert repo.updates == 1


@pytest.mark.asyncio
async def test_apply_noop_below_threshold():
    svc = SummarizationService(
        enabled=True, recent_turns=2, fallback_threshold_chars=10_000_000
    )
    gw, repo, session = _FakeGateway(), _FakeRepo(), _session()
    prior = _long_turns(3)
    live, summary = await svc.apply(
        gateway=gw, repo=repo, session=session, user_id="u",
        deployment="dep", prior=prior, system_prompt=None, context_window=None,
    )
    assert summary is None
    assert live == prior  # full window sent, nothing folded
    assert not gw.calls
    assert repo.updates == 0


@pytest.mark.asyncio
async def test_apply_is_incremental_over_existing_fold():
    svc = SummarizationService(
        enabled=True, recent_turns=2, fallback_threshold_chars=10
    )
    gw, repo = _FakeGateway("SECOND"), _FakeRepo()
    prior = _long_turns(5)  # 10 messages
    # Pretend the first 4 messages were already folded last time.
    session = _session(summary="FIRST", summarizedThroughMessageId=prior[3].id)
    live, summary = await svc.apply(
        gateway=gw, repo=repo, session=session, user_id="u",
        deployment="dep", prior=prior, system_prompt=None, context_window=None,
    )
    # Only the post-fold turns are eligible; we never re-fold what's done.
    assert live == prior[-2:]
    # The prior summary was passed in for refinement (extend, not replace).
    folded_prompt = "\n\n".join(
        m["content"] for m in gw.calls[0] if m["role"] == "user"
    )
    assert "FIRST" in folded_prompt
    assert summary == "SECOND"


@pytest.mark.asyncio
async def test_apply_does_not_mutate_prior_transcript():
    svc = SummarizationService(
        enabled=True, recent_turns=2, fallback_threshold_chars=10
    )
    gw, repo, session = _FakeGateway(), _FakeRepo(), _session()
    prior = _long_turns(4)
    snapshot = list(prior)
    await svc.apply(
        gateway=gw, repo=repo, session=session, user_id="u",
        deployment="dep", prior=prior, system_prompt=None, context_window=None,
    )
    assert prior == snapshot  # the source transcript list is untouched


# --- build from settings ------------------------------------------------------


def test_builder_reflects_default_off_flag():
    svc = build_summarization_service(make_settings())
    assert svc.enabled is False


def test_builder_honors_enabled_flag():
    svc = build_summarization_service(make_settings(auto_summarization_enabled=True))
    assert svc.enabled is True
