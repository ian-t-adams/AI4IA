"""Sequential bounded workers, complete reports, and conservative comparisons."""
from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from .contracts import (
    MAX_CASE_SECONDS, MAX_REPORT_BYTES, ORACLE_VERSION, ROOT, RUNNER_VERSION,
    Case, CaseResult, Check, Comparison, Dataset, EvaluationError, Identity, Report,
    applicable_checks, bounded_json, canonical_bytes, decode_json, digest, load_dataset,
    make_report, overall, unknown_result,
)
from .offline import GATEWAY_FIXTURE_VERSION, offline_environment, resolve_models, settings_for

Executor = Callable[[Dataset, Case], CaseResult | None]


def _tree_digest(files: list[Path]) -> str:
    hashed = hashlib.sha256()
    for path in sorted(files):
        hashed.update(path.relative_to(ROOT).as_posix().encode())
        hashed.update(b"\x00")
        hashed.update(hashlib.sha256(path.read_bytes()).digest())
    return hashed.hexdigest()


def build_identity(dataset: Dataset) -> Identity:
    git = subprocess.run(
        ["git", "--no-pager", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, timeout=10,
        check=True,
        env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
    )
    revision = git.stdout.decode("ascii").strip()
    with offline_environment():
        models = resolve_models(dataset)
        config = {
            "fixture": dataset.config.model_dump(mode="json"),
            "settings": {case.id: settings_for(case).model_dump(mode="json") for case in dataset.cases},
        }
    source = ROOT / "app" / "api" / "src" / "ai4ia_api"
    evaluator_files = list(Path(__file__).parent.glob("*.py")) + [
        ROOT / "app" / "api" / "tests" / "conftest.py",
    ]
    return Identity(
        runner_version=RUNNER_VERSION, oracle_version=ORACLE_VERSION,
        dataset_id=dataset.id, dataset_version=dataset.version,
        dataset_sha256=digest(dataset.model_dump(mode="json")),
        config_id=dataset.config.id, config_version=dataset.config.version,
        config_sha256=digest(config),
        prompt_version=dataset.prompt_version,
        prompt_sha256=digest([
            (case.id, case.input, case.system_prompt, case.workflow_instructions)
            for case in dataset.cases
        ]),
        price_fixture_version=dataset.config.price_version,
        gateway_fixture_version=GATEWAY_FIXTURE_VERSION,
        catalog_sha256=_tree_digest([
            ROOT / "infra" / "models.json", source / "data" / "model_catalog.json",
        ]),
        evaluator_sha256=_tree_digest(evaluator_files),
        application_revision=revision,
        application_tree_sha256=_tree_digest([
            path for path in source.rglob("*") if path.suffix in (".py", ".json")
        ]),
        environment_sha256=digest({
            "python": platform.python_version(),
            "packages": {name: version(name) for name in (
                "fastapi", "starlette", "pydantic", "pydantic-settings", "httpx", "pytest", "jsonschema",
            )},
        }),
        case_ids=tuple(case.id for case in dataset.cases),
        models=tuple(model.identity for model in models.values()),
    )


def worker_environment() -> dict[str, str]:
    return {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}


def run_case_subprocess(dataset: Dataset, case: Case) -> CaseResult:
    # Isolated mode drops user-site/PYTHONPATH startup hooks; an absolute source
    # root is inserted explicitly, never inferred from an inherited import path.
    bootstrap = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from scripts.evaluations.__main__ import worker; worker()"
    )
    request = canonical_bytes({
        "case_id": case.id, "dataset_sha256": digest(dataset.model_dump(mode="json")),
    })
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", bootstrap, str(ROOT)],
            cwd=ROOT, input=request, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=MAX_CASE_SECONDS, env=worker_environment(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return unknown_result(case, "timeout")
    except OSError:
        return unknown_result(case, "execution_error")
    if completed.returncode != 0:
        return unknown_result(case, "execution_error")
    if not completed.stdout:
        return unknown_result(case, "missing_outcome")
    try:
        result = CaseResult.model_validate(decode_json(
            completed.stdout, 16_384, "worker_output_too_large",
        ))
    except (ValidationError, EvaluationError):
        return unknown_result(case, "invalid_outcome")
    if result.case_id != case.id or result.protocol != case.protocol:
        return unknown_result(case, "invalid_outcome")
    return result


def evaluate(dataset: Dataset, *, executor: Executor = run_case_subprocess) -> Report:
    identity = build_identity(dataset)
    rows = []
    for case in dataset.cases:
        result = executor(dataset, case)
        if result is None:
            result = unknown_result(case, "missing_outcome")
        elif result.case_id != case.id or result.protocol != case.protocol:
            result = unknown_result(case, "invalid_outcome")
        elif result.status != "unscored":
            active = applicable_checks(case)
            checks = tuple(
                Check(id=check.id, status="unknown", reason="evidence_missing")
                if check.id in active and check.status == "unscored" else check
                for check in result.checks
            )
            result = CaseResult(
                **{**result.model_dump(), "checks": checks,
                   "status": overall([check.status for check in checks])},
            )
        rows.append(result)
    return make_report(identity, rows)


def write_report(report: Report, path: Path) -> None:
    validated = Report.model_validate(report.model_dump(mode="json"))
    payload = canonical_bytes(validated.model_dump(mode="json")) + b"\n"
    if len(payload) > MAX_REPORT_BYTES:
        raise EvaluationError("report_too_large")
    # Explicit destination only. Exclusive create refuses symlinks and existing
    # files rather than overwriting a baseline, source file, or somebody's work.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)


def read_report(path: Path) -> Report:
    return Report.model_validate(bounded_json(path, MAX_REPORT_BYTES, "report_too_large"))


def compare_reports(
    baseline: Report, candidate: Report, *, dataset: Dataset | None = None,
) -> Comparison:
    baseline = Report.model_validate(baseline.model_dump(mode="json"))
    candidate = Report.model_validate(candidate.model_dump(mode="json"))
    dataset = dataset or load_dataset()
    expected_ids = tuple(case.id for case in dataset.cases)
    expected_digest = digest(dataset.model_dump(mode="json"))
    for report in (baseline, candidate):
        if (
            report.identity.case_ids != expected_ids
            or report.identity.dataset_sha256 != expected_digest
            or any(
                row.protocol != case.protocol for row, case in zip(report.cases, dataset.cases)
            )
        ):
            raise EvaluationError("incompatible_dataset")
        for case, result in zip(dataset.cases, report.cases):
            if result.status == "passed" and any(
                check.id in applicable_checks(case) and check.status == "unscored"
                for check in result.checks
            ):
                raise EvaluationError("incompatible_scoring_coverage")
    ignored = {"application_revision", "application_tree_sha256"}
    if baseline.identity.model_dump(exclude=ignored) != candidate.identity.model_dump(exclude=ignored):
        raise EvaluationError("incompatible_run_identity")
    regressed, improved, unknown = [], [], []
    rank = {"passed": 3, "unknown": 1, "unscored": 1, "failed": 0}
    for before, after in zip(baseline.cases, candidate.cases):
        if (
            before.instruction_hashes and after.instruction_hashes
            and before.instruction_hashes != after.instruction_hashes
        ):
            raise EvaluationError("incompatible_instruction_version")
        if any(rank[b.status] > rank[a.status] for b, a in zip(before.checks, after.checks)):
            regressed.append(after.case_id)
        if any(rank[b.status] < rank[a.status] for b, a in zip(before.checks, after.checks)):
            improved.append(after.case_id)
        if after.status in ("unknown", "unscored"):
            unknown.append(after.case_id)
    return Comparison(
        case_count=len(expected_ids), regressed=regressed, improved=improved, unknown=unknown,
    )
