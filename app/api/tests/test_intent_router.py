"""Deterministic intent router (Phase 11C).

The router decides *whether to offer* the compute/export tools. The load-bearing
invariant is that Code Interpreter is **never the default**: only an explicit
compute/tabular or "adjust & return" signal routes away from ``qa``. Everything
here is pure and offline.
"""
from __future__ import annotations

import pytest

from ai4ia_api.library.router import Intent, IntentRouter


@pytest.fixture
def router() -> IntentRouter:
    return IntentRouter()


# --- qa is the default front door ---
@pytest.mark.parametrize(
    "text",
    [
        "What does this contract say about termination?",
        "Summarize the document for me.",
        "Who are the parties named in the agreement?",
        "Explain section 4 in plain english.",
        "What do you mean by force majeure?",
        "Cite the clause about confidentiality.",
        "Does the report mention any risks?",
        "",
        "   ",
    ],
)
def test_qa_is_the_default(router: IntentRouter, text: str):
    decision = router.classify(text)
    assert decision.intent is Intent.qa
    assert decision.offers_compute is False


# --- compute branch ---
@pytest.mark.parametrize(
    "text",
    [
        "Calculate the total revenue across all quarters.",
        "What's the average salary in the spreadsheet?",
        "How many invoices are overdue?",
        "Plot a bar chart of sales by region.",
        "Group the rows by department and count them.",
        "Compute the standard deviation of the prices.",
        "Sum the amounts in the expenses column.",
        "Give me the correlation between price and demand.",
        "Build a pivot table from this data.",
        "What is the maximum value in column B?",
    ],
)
def test_compute_signals_route_to_compute(router: IntentRouter, text: str):
    decision = router.classify(text)
    assert decision.intent is Intent.compute
    assert decision.offers_compute is True
    assert decision.signals  # at least one signal recorded


# --- transform / "adjust & return" branch ---
@pytest.mark.parametrize(
    "text",
    [
        "Export this as a clean markdown file.",
        "Save it as a new version with the totals filled in.",
        "Convert the document to a CSV.",
        "Translate the contract into Spanish and give it back.",
        "Redact all the personal information and return a new copy.",
        "Rewrite the summary and save a new version.",
        "Create a new file with the cleaned-up data.",
        "Reformat this into a one-page report I can download.",
    ],
)
def test_transform_signals_route_to_transform(router: IntentRouter, text: str):
    decision = router.classify(text)
    assert decision.intent is Intent.transform
    assert decision.offers_compute is True


def test_transform_wins_over_compute_when_both_present(router: IntentRouter):
    # "calculate the totals" (compute) + "save them as a new sheet" (transform):
    # the defining feature is a new artifact, so transform takes precedence.
    decision = router.classify("Calculate the totals and save them as a new sheet.")
    assert decision.intent is Intent.transform


def test_short_words_do_not_fire_inside_larger_words(router: IntentRouter):
    # "sum" must not match "summary"; "min" must not match "minutes"; "max" must
    # not match a name like "Max"; "count" must not match "country".
    for text in (
        "Give me a summary of the meeting minutes.",
        "Tell me about the country described here.",
        "What did Max say in the email?",
    ):
        assert router.classify(text).intent is Intent.qa


def test_classify_is_pure_and_repeatable(router: IntentRouter):
    text = "Calculate the total."
    assert router.classify(text).intent is router.classify(text).intent
