"""Behavioral evaluation controls use real app execution, not golden report replay."""
from __future__ import annotations

import json
import os
import socket
import subprocess
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError

from scripts.evaluations.contracts import (
    CHECK_IDS,
    MAX_CASES,
    MAX_REPORT_BYTES,
    Case,
    CaseResult,
    Check,
    Dataset,
    EvaluationError,
    Report,
    load_dataset,
    make_report,
)
from scripts.evaluations.offline import execute_case, offline_environment
from scripts.evaluations.oracles import score_case
from scripts.evaluations.runner import (
    compare_reports,
    evaluate,
    read_report,
    run_case_subprocess,
    write_report,
    worker_environment,
)


@pytest.fixture(scope="module")
def dataset():
    return load_dataset()


def case_named(dataset, name):
    return next(case for case in dataset.cases if case.id == name)


def changed_case(case, change):
    raw = deepcopy(case.model_dump(mode="json"))
    change(raw)
    return Case.model_validate(raw)


@pytest.fixture(scope="module")
def golden_report(dataset):
    return evaluate(dataset, executor=execute_case)


def test_golden_controls_execute_every_case(dataset, golden_report):
    assert golden_report.coverage.total == len(dataset.cases)
    assert golden_report.coverage.passed == len(dataset.cases)
    assert golden_report.coverage.failed == golden_report.coverage.unknown == 0
    assert golden_report.gate == "passed"
    assert [row.case_id for row in golden_report.cases] == [case.id for case in dataset.cases]
    assert {case.scenario for case in dataset.cases} >= {
        "chat", "agent", "workflow", "document", "approval", "safety",
    }
    assert {case.protocol for case in dataset.cases} == {"chat", "responses", "anthropic"}
    for row in golden_report.cases:
        assert tuple(check.id for check in row.checks) == CHECK_IDS
        assert row.measurements.model_calls > 0
        assert row.measurements.cost_micro_usd is not None
        assert row.instruction_hashes
    for coverage in golden_report.check_coverage.values():
        assert coverage.total == len(dataset.cases)
        assert coverage.scored + coverage.unknown + coverage.unscored == coverage.total


@pytest.mark.parametrize(
    "name, mutate, check",
    [
        (
            "agent-chat",
            lambda c: c["replies"][0]["body"]["choices"][0]["message"]["tool_calls"][0][
                "function"
            ].update(name="current_time"),
            "tool_choice",
        ),
        (
            "document-owned",
            lambda c: c["replies"][0]["body"]["choices"][0]["message"].update(
                content="Falcon shipped in March [[cite:S99]]."
            ),
            "citations",
        ),
        (
            "chat-json",
            lambda c: c["replies"][0]["body"]["choices"][0]["message"].update(
                content='{"answer":"forty-two"}'
            ),
            "output_schema",
        ),
        (
            "safety-refusal",
            lambda c: c["replies"][0]["body"]["content"][0].update(
                text="Here is the private record."
            ),
            "content",
        ),
        (
            "safety-annotations",
            lambda c: c["replies"][0]["body"]["choices"][0]["content_filter_results"][
                "violence"
            ].update(filtered=True),
            "safety",
        ),
        (
            "agent-chat",
            lambda c: c["replies"][0].update(latency_ms=1000),
            "latency",
        ),
        (
            "chat-json",
            lambda c: c["replies"][0]["body"].update(
                usage={"prompt_tokens": 10000, "completion_tokens": 10000, "total_tokens": 20000}
            ),
            "cost",
        ),
    ],
)
def test_seeded_model_regressions_fail_with_identical_control(dataset, name, mutate, check):
    original = case_named(dataset, name)
    control = execute_case(dataset, original)
    seeded = execute_case(dataset, changed_case(original, mutate))
    assert control.status == "passed"
    assert seeded.status == "failed"
    assert next(item for item in seeded.checks if item.id == check).status == "failed"


def test_actual_approval_bypass_is_detected(dataset):
    case = case_named(dataset, "approval-exact-replay")
    assert execute_case(dataset, case).status == "passed"
    from ai4ia_api.agents import runtime

    with patch.object(runtime, "requires_invocation_approval", return_value=False):
        seeded = execute_case(dataset, case)
    assert seeded.status == "failed"
    assert next(check for check in seeded.checks if check.id == "approval").status == "failed"


@pytest.mark.parametrize("missing", ["usage", "pricing"])
def test_missing_usage_or_pricing_is_unknown_not_zero(dataset, missing):
    case = case_named(dataset, "chat-json")
    if missing == "usage":
        case = changed_case(case, lambda raw: raw["replies"][0]["body"].pop("usage"))
    else:
        raw = dataset.model_dump(mode="json")
        raw["config"]["pricing_enabled"] = False
        dataset = Dataset.model_validate(raw)
    result = execute_case(dataset, case)
    assert result.status == "unknown"
    assert result.measurements.cost_micro_usd is None
    assert result.measurements.cost_coverage == "unknown"
    assert next(check for check in result.checks if check.id == "cost").status == "unknown"


@pytest.mark.parametrize("fault", ["foreign_owner", "wrong_revision", "missing_feedback"])
def test_receipt_and_delivery_regressions_cannot_self_certify(dataset, fault):
    case = case_named(dataset, "agent-chat" if fault == "missing_feedback" else "document-owned")
    assert execute_case(dataset, case).status == "passed"

    def corrupted_evidence(data, item, observed):
        if fault == "missing_feedback":
            for request in observed.requests:
                request["messages"] = [
                    message for message in request.get("messages", []) if message.get("role") != "tool"
                ]
        else:
            source = observed.messages[-1]["sources"][0]
            if fault == "foreign_owner":
                source["documentId"] = next(iter(observed.foreign_sources))
                observed.messages[-1]["citations"][0]["documentId"] = source["documentId"]
            else:
                source["documentVersion"] = "0" * 64
        return score_case(data, item, observed)

    with patch("scripts.evaluations.offline.score_case", side_effect=corrupted_evidence):
        seeded = execute_case(dataset, case)
    name = "tool_feedback" if fault == "missing_feedback" else "citations"
    assert seeded.status == "failed"
    assert next(check for check in seeded.checks if check.id == name).status == "failed"


@pytest.mark.parametrize("reply", ["not JSON", "NaN", '{"answer":42,"answer":0}'])
def test_invalid_json_cannot_pass_a_nullable_or_permissive_schema(dataset, reply):
    case = case_named(dataset, "chat-json")

    def permissive(raw):
        raw["expected"]["output_schema"] = {}
        raw["expected"]["required_text"] = []
        raw["replies"][0]["body"]["choices"][0]["message"]["content"] = "null"

    control = changed_case(case, permissive)
    assert execute_case(dataset, control).status == "passed"
    seeded = execute_case(dataset, changed_case(
        control, lambda raw: raw["replies"][0]["body"]["choices"][0]["message"].update(content=reply),
    ))
    assert next(check for check in seeded.checks if check.id == "output_schema").status == "failed"


def test_no_outcomes_and_failures_stay_in_denominator(dataset):
    failed_case = case_named(dataset, "chat-json")
    failed = execute_case(dataset, changed_case(
        failed_case,
        lambda raw: raw["replies"][0]["body"]["choices"][0]["message"].update(content="not JSON"),
    ))

    def incomplete_executor(_dataset, case):
        return failed if case.id == failed_case.id else None

    report = evaluate(dataset, executor=incomplete_executor)
    assert report.gate == "failed"
    assert report.coverage.total == len(dataset.cases)
    assert report.coverage.failed == 1
    assert report.coverage.unknown == len(dataset.cases) - 1
    assert report.coverage.pass_numerator == 0
    assert report.coverage.pass_denominator == len(dataset.cases)


@pytest.mark.parametrize("failure", ["timeout", "crash", "invalid", "empty"])
def test_worker_failure_is_an_unknown_row(dataset, failure):
    case = dataset.cases[0]
    if failure == "timeout":
        outcome = subprocess.TimeoutExpired("synthetic-worker", 1)
        kwargs = {"side_effect": outcome}
    else:
        output = b'{"raw_prompt":"must-not-leak"}' if failure == "invalid" else b""
        kwargs = {"return_value": subprocess.CompletedProcess(
            args=[], returncode=1 if failure == "crash" else 0, stdout=output, stderr=b"private",
        )}
    with patch("scripts.evaluations.runner.subprocess.run", **kwargs):
        result = run_case_subprocess(dataset, case)
    assert result.case_id == case.id
    assert result.status == "unknown"
    assert result.measurements.cost_micro_usd is None
    assert "private" not in result.model_dump_json()
    assert "raw_prompt" not in result.model_dump_json()


def test_worker_subprocess_control_executes_real_app(dataset):
    with patch.dict(os.environ, {
        "HTTPS_PROXY": "http://must-not-use.invalid",
        "APPLICATIONINSIGHTS_CONNECTION_STRING": "must-not-export",
        "AI4IA_SESSION_STORE": "cosmos",
        "AZURE_CLIENT_SECRET": "must-not-read",
        "PYTHONPATH": "must-not-import",
    }):
        result = run_case_subprocess(dataset, case_named(dataset, "agent-responses"))
    assert result.status == "passed"
    assert result.measurements.model_calls == 2


def test_real_stalled_worker_is_terminated_and_retained(dataset):
    real_run = subprocess.run

    def stalled(_command, **kwargs):
        return real_run(
            [os.sys.executable, "-c", "import time; time.sleep(10)"],
            **{**kwargs, "timeout": 0.1},
        )

    with patch("scripts.evaluations.runner.subprocess.run", side_effect=stalled):
        result = run_case_subprocess(dataset, dataset.cases[0])
    assert result.status == "unknown"
    assert all(check.reason == "timeout" for check in result.checks)


def test_environment_and_network_are_isolated_with_mock_control():
    poisoned = {
        "HTTP_PROXY": "http://private-proxy.invalid",
        "HTTPS_PROXY": "http://private-proxy.invalid",
        "APPLICATIONINSIGHTS_CONNECTION_STRING": "must-not-export",
        "AZURE_CLIENT_SECRET": "must-not-read",
        "AI4IA_SESSION_STORE": "cosmos",
        "PYTHONPATH": "must-not-import",
    }
    with patch.dict(os.environ, poisoned), offline_environment():
        assert not any(key in os.environ for key in poisoned)
        with pytest.raises(EvaluationError, match="offline_network_denied"):
            httpx.get("https://example.invalid", timeout=0.1)
        with socket.socket() as connection:
            with pytest.raises(EvaluationError, match="offline_network_denied"):
                connection.connect(("192.0.2.1", 443))
        with httpx.Client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"synthetic": True})
        )) as client:
            assert client.get("https://example.invalid").json() == {"synthetic": True}
    assert os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING") != "must-not-export"


@pytest.mark.parametrize("allow_dotenv", [False, True])
@pytest.mark.parametrize("dotenv_location", ["cwd", "source-adjacent"])
def test_import_time_dotenv_cannot_initialize_an_exporter(tmp_path, allow_dotenv, dotenv_location):
    # Only the owned temporary dotenv is ever available to the unprotected
    # control, and an inert spy replaces export in BOTH processes.
    (tmp_path / ".env").write_text(
        "APPLICATIONINSIGHTS_CONNECTION_STRING=synthetic-dotenv-cwd\n", encoding="utf-8",
    )
    adjacent = tmp_path / "app" / "api" / ".env"
    adjacent.parent.mkdir(parents=True)
    adjacent.write_text(
        "APPLICATIONINSIGHTS_CONNECTION_STRING=synthetic-dotenv-source-adjacent\n", encoding="utf-8",
    )
    source = """
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from pydantic_settings.sources import DotEnvSettingsSource
from scripts.evaluations.contracts import load_dataset
from scripts.evaluations.offline import offline_environment, settings_for
original = DotEnvSettingsSource._read_env_files
with offline_environment():
    from ai4ia_api import config, logging_setup
    if sys.argv[3] == "source-adjacent":
        config.Settings.model_config["env_file"] = str(Path.cwd() / "app" / "api" / ".env")
    seen = []
    with ExitStack() as stack:
        stack.enter_context(patch.object(logging_setup, "configure_telemetry", side_effect=seen.append))
        if sys.argv[2] == "control":
            stack.enter_context(patch.object(DotEnvSettingsSource, "_read_env_files", original))
        settings = settings_for(load_dataset().cases[0])
        assert settings.applicationinsights_connection_string is None
print(json.dumps({
    "startup_received_dotenv": ("synthetic-dotenv-" + sys.argv[3]) in seen,
    "unexpected_config": any(value not in (None, "synthetic-dotenv-" + sys.argv[3]) for value in seen),
    "startup_calls": len(seen),
}))
"""
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [os.sys.executable, "-I", "-B", "-c", source, str(root),
         "control" if allow_dotenv else "protected", dotenv_location],
        cwd=tmp_path, env=worker_environment(), capture_output=True, timeout=30, check=True,
    )
    evidence = json.loads(completed.stdout)
    assert evidence["startup_calls"] == 1
    assert evidence["unexpected_config"] is False
    assert evidence["startup_received_dotenv"] is allow_dotenv


def test_report_has_only_content_free_fields_and_no_default_artifact(dataset, golden_report):
    serialized = golden_report.model_dump_json()
    for forbidden in (
        "userId", "sessionId", "correlationId", "raw_prompt", "response_body",
        "tool_payload", "grant", "Falcon shipped", "You are", "@evalcalc",
        "courier.example", "dev@example", "PRIVATE-SYNTHETIC-CANARY",
    ):
        assert forbidden not in serialized
    assert len(serialized.encode()) <= MAX_REPORT_BYTES
    assert golden_report.identity.mode == "offline-synthetic"
    assert golden_report.identity.live_quality == "not_measured"
    assert golden_report.identity.judge == "disabled"
    assert all(model.provider_fixture_version for model in golden_report.identity.models)
    assert all(model.catalog_model_version for model in golden_report.identity.models)


def test_comparison_allows_identified_source_changes_but_rejects_version_drift(golden_report):
    same_contract = golden_report.model_copy(deep=True)
    same_contract.identity.application_revision = "a" * 40
    assert compare_reports(golden_report, same_contract).regressed == []
    for field in (
        "dataset_sha256", "config_sha256", "prompt_sha256", "evaluator_sha256",
        "catalog_sha256",
    ):
        incompatible = golden_report.model_copy(deep=True)
        setattr(incompatible.identity, field, "b" * 64)
        with pytest.raises(EvaluationError, match="incompatible"):
            compare_reports(golden_report, incompatible)
    for field in ("catalog_model_version", "provider_fixture_version"):
        incompatible = golden_report.model_copy(deep=True)
        setattr(incompatible.identity.models[0], field, "different-version")
        with pytest.raises(EvaluationError, match="incompatible"):
            compare_reports(golden_report, incompatible)


def test_report_rejects_unidentified_duplicated_missing_or_falsified_results(golden_report):
    raw = golden_report.model_dump(mode="json")
    changes = [
        lambda value: value["identity"]["models"][0].pop("catalog_model_version"),
        lambda value: value["identity"].update(application_revision="unknown"),
        lambda value: value["identity"]["models"].pop(),
        lambda value: value["cases"].pop(),
        lambda value: value["cases"].append(deepcopy(value["cases"][0])),
        lambda value: value["coverage"].update(failed=17),
        lambda value: value["cases"][0]["checks"].pop(),
        lambda value: value["cases"][0].update(raw_prompt="private"),
    ]
    for change in changes:
        malformed = deepcopy(raw)
        change(malformed)
        with pytest.raises((ValidationError, EvaluationError)):
            Report.model_validate(malformed)


def test_self_consistent_filtered_report_cannot_hide_missing_cases(golden_report):
    identity = golden_report.identity.model_copy(deep=True)
    identity.case_ids = identity.case_ids[:-1]
    filtered = make_report(identity, list(golden_report.cases[:-1]))
    with pytest.raises(EvaluationError, match="incompatible_dataset"):
        compare_reports(filtered, filtered)


def test_unscored_outcomes_are_not_successes_or_removed(dataset):
    def no_oracles(_dataset, case):
        return CaseResult(
            case_id=case.id, protocol=case.protocol, status="unscored",
            checks=tuple(
                Check(id=name, status="unscored", reason="not_run") for name in CHECK_IDS
            ),
        )

    report = evaluate(dataset, executor=no_oracles)
    assert report.gate == "unknown"
    assert report.coverage.unscored == report.coverage.total == len(dataset.cases)
    assert report.coverage.scored == report.coverage.pass_numerator == 0
    assert report.coverage.pass_denominator == len(dataset.cases)


def test_partial_scoring_cannot_turn_a_required_oracle_into_an_inapplicable_pass(dataset, golden_report):
    by_id = {case.case_id: case for case in golden_report.cases}

    def omit_cost(_dataset, case):
        result = by_id[case.id]
        checks = tuple(
            Check(id=check.id, status="unscored", reason="not_applicable")
            if check.id == "cost" else check for check in result.checks
        )
        return CaseResult(**{**result.model_dump(), "checks": checks})

    report = evaluate(dataset, executor=omit_cost)
    assert report.gate == "unknown"
    assert report.coverage.unknown == report.coverage.total
    assert report.check_coverage["cost"].unknown == len(dataset.cases)

@pytest.mark.parametrize("invalid", ["duplicates", "too_many", "oversized", "references", "live"])
def test_dataset_bounds_and_policy_fail_closed(dataset, invalid):
    raw = dataset.model_dump(mode="json")
    if invalid == "duplicates":
        raw["cases"].append(deepcopy(raw["cases"][0]))
    elif invalid == "too_many":
        raw["cases"] = [
            {**deepcopy(raw["cases"][0]), "id": f"case-{index}"} for index in range(MAX_CASES + 1)
        ]
    elif invalid == "oversized":
        raw["cases"][0]["replies"][0]["body"]["padding"] = "x" * 20000
    elif invalid == "references":
        raw["cases"][0]["expected"]["output_schema"] = {"$ref": "https://example.invalid/schema"}
    else:
        raw["config"]["mode"] = "live"
    with pytest.raises((ValidationError, EvaluationError)):
        Dataset.model_validate(raw)


def test_report_artifact_is_bounded_validated_and_never_overwritten(tmp_path, golden_report):
    path = tmp_path / "report.json"
    write_report(golden_report, path)
    assert read_report(path) == golden_report
    with pytest.raises(FileExistsError):
        write_report(golden_report, path)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (MAX_REPORT_BYTES + 1))
    with pytest.raises(EvaluationError, match="report_too_large"):
        read_report(oversized)


@pytest.mark.parametrize(
    "args", [
        ["run", "--mode", "live"],
        ["run", "--judge", "paid-model"],
        ["run", "--production-content"],
    ],
)
def test_unapproved_live_judge_and_content_modes_refuse_before_execution(args):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [os.sys.executable, "-m", "scripts.evaluations", *args],
        cwd=root, capture_output=True, timeout=30,
    )
    assert result.returncode == 2
    assert b"approval_required" in result.stderr
    assert not result.stdout
