"""Opt-in, finite authored-synthetic evaluations through the governed API only."""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import ValidationError

from .contracts import (
    ROOT, EvaluationError, bounded_json, canonical_bytes, coverage, decode_json, digest,
)
from .live_contracts import (
    LIVE_CHECK_IDS, MAX_LIVE_REPORT_BYTES, MAX_OUTPUT_TOKENS,
    MAX_RECONCILES, MAX_RUN_SECONDS, TOKEN_ENV, FailureCode, LiveCase, LiveConfig,
    ExecutionCapabilities, LifecycleObservation, LiveDataset, LiveError, LiveIdentity,
    LiveReport, LiveResult, bind_token,
    config_from_environment, limits, load_live_dataset, source_documents, source_model, unknown_live,
)
from .live_http import HTTPS, ApiClient, Budget
from .live_oracles import object_value, score_live, with_cleanup

_REVISION_ENV = "_AI4IA_LIVE_EVAL_SOURCE_REVISION"
_WORKER_CONFIG_ENV = "_AI4IA_LIVE_EVAL_CONFIG"
_WORKER_OPERATION_ENV = "_AI4IA_LIVE_EVAL_OPERATION"
# These request-reduction controls are implemented by the governed API, not by
# the caller's preflight. Their schema must be observed before creating fixtures.
REQUEST_CONTROLS = {"allowTools": False, "allowAutomaticMemory": False, "requireFreshSession": True}


def revision() -> str:
    executable = shutil.which("git")
    if executable is None:
        raise LiveError("configuration")
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "PATH") if key in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    result = subprocess.run(
        [executable, "--no-pager", "rev-parse", "HEAD"], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True,
    )
    return result.stdout.decode("ascii").strip()


def build_identity(dataset: LiveDataset, source_revision: str, config: LiveConfig | None = None) -> LiveIdentity:
    catalog, prices = source_documents()
    files = sorted(
        path for folder in (
            ROOT / "scripts" / "evaluations", ROOT / "app" / "api" / "src" / "ai4ia_api",
        ) for path in folder.rglob("*") if path.suffix in (".py", ".json")
    )
    if len(files) > 4096 or sum(path.stat().st_size for path in files) > 32 * 1024 * 1024:
        raise LiveError("bounds")
    source = {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    }
    selected = source_model(config, catalog, prices) if config is not None else None
    return LiveIdentity(
        dataset_id=dataset.id, dataset_version=dataset.version, prompt_version=dataset.prompt_version,
        dataset_sha256=digest(dataset.model_dump(mode="json")),
        source_revision=source_revision, source_sha256=digest(source),
        catalog_sha256=digest(catalog), pricing_sha256=digest(prices),
        environment_sha256=digest({
            "python": platform.python_version(),
            "packages": {name: version(name) for name in ("pydantic", "httpx", "jsonschema")},
        }),
        config_sha256=config.public_digest() if config is not None else None,
        model_id=config.model_id if config is not None else None,
        catalog_model_versions=tuple(sorted({
            deployment["version"] for deployment in selected["deployments"]
        })) if selected is not None else (),
        case_ids=tuple(case.id for case in dataset.cases),
    )


def make_live_report(
    identity: LiveIdentity, rows: list[LiveResult], budget: Budget | None, termination: FailureCode | str,
    lifecycle: LifecycleObservation | None = None,
    *, operation: Literal["run", "preflight"] = "run", api_preflight: str = "not_run",
) -> LiveReport:
    lifecycle = lifecycle or LifecycleObservation()
    summary = coverage([row.status for row in rows])
    complete = operation == "run" and api_preflight == "passed" and termination == "complete" and lifecycle.status == "passed" and all(
        all(check.status != "unknown" for check in row.checks)
        and row.checks[-1].status == "passed" for row in rows
    )
    return LiveReport.model_validate({
        "identity": identity, "cases": tuple(rows), "coverage": summary,
        "check_coverage": {
            name: coverage([row.checks[index].status for row in rows])
            for index, name in enumerate(LIVE_CHECK_IDS)
        },
        "complete": complete, "gate": "unknown" if not complete else "failed" if summary.failed else "passed",
        "termination": termination, "http_attempts": budget.http_attempts if budget else None,
        "response_bytes": budget.response_bytes if budget else None, "limits": limits(),
        "lifecycle": lifecycle,
        "operation": operation, "api_preflight": api_preflight,
    })


def _expect(client: ApiClient, method: str, path: str, body: dict | None = None, *, status: int = 200) -> object:
    result = client.request(method, path, body)
    if result.status != status:
        raise LiveError("http")
    return result.json()


def preflight_api(client: ApiClient, config: LiveConfig) -> tuple[str, ExecutionCapabilities]:
    catalog, prices = source_documents()
    expected = source_model(config, catalog, prices)
    advertised = object_value(_expect(client, "GET", "/api/models"))
    models = advertised.get("models")
    if not isinstance(models, list):
        raise LiveError("catalog")
    matching = [item for item in models if isinstance(item, dict) and item.get("id") == config.model_id]
    if (
        len(matching) != 1 or matching[0].get("api") != "chat"
        or matching[0].get("supportsSampling") is not True
        or type(matching[0].get("maxOutputTokens")) is not int
        or matching[0]["maxOutputTokens"] < MAX_OUTPUT_TOKENS
        or not isinstance(matching[0].get("options"), list) or not matching[0]["options"]
    ):
        # The Responses output floor and reasoning adapters are not treated as
        # small-output-capped live paths.
        raise LiveError("catalog")
    generated = object_value(bounded_json(
        ROOT / "app" / "api" / "src" / "ai4ia_api" / "data" / "model_catalog.json",
        262_144, "catalog_too_large",
    ))
    local_models = generated.get("models")
    local = [
        model for model in local_models if isinstance(model, dict) and model.get("id") == config.model_id
    ] if isinstance(local_models, list) else []
    if len(local) != 1 or matching[0].get("category") != expected.get("category"):
        raise LiveError("catalog")
    local_options = local[0].get("options")
    if not isinstance(local_options, list):
        raise LiveError("catalog")
    projected_options = []
    for option in matching[0]["options"]:
        if not isinstance(option, dict):
            raise LiveError("catalog")
        projected = {key: option.get(key) for key in ("region", "sku", "deploymentName")}
        if projected not in [
            {key: row.get(key) for key in projected}
            for row in local_options if isinstance(row, dict)
        ]:
            raise LiveError("catalog")
        versions = {
            deployment.get("version") for deployment in expected["deployments"]
            if deployment.get("region") == projected["region"] and deployment.get("sku") == projected["sku"]
        }
        if len(versions) != 1 or option.get("modelVersion") not in (None, *versions):
            raise LiveError("catalog")
        if projected in projected_options:
            raise LiveError("catalog")
        projected_options.append(projected)
    proof = client.request("POST", "/api/chat", {name: {} for name in REQUEST_CONTROLS})
    if proof.status != 422:
        raise LiveError("capability")
    errors = object_value(proof.json()).get("detail")
    locations = [
        error.get("loc") for error in errors if isinstance(error, dict)
    ] if isinstance(errors, list) else []
    if not all(["body", name] in locations for name in (*REQUEST_CONTROLS, "content", "sessionId")):
        raise LiveError("capability")
    capability_body = _expect(
        client, "GET",
        "/api/execution-capabilities?profile=authored-synthetic-evaluation&model="
        + quote(config.model_id, safe=""),
    )
    try:
        capabilities = ExecutionCapabilities.model_validate(capability_body)
    except ValidationError:
        raise LiveError("capability") from None
    if (
        capabilities.model != config.model_id
        or capabilities.region not in {option["region"] for option in projected_options}
    ):
        raise LiveError("capability")
    # Only project the chosen model's catalog metadata, never arbitrary response
    # fields, claims, or trace identifiers into the report.
    return digest({
        "id": config.model_id, "api": "chat", "maxOutputTokens": matching[0]["maxOutputTokens"],
        "options": projected_options, "supportsSampling": True,
    }), capabilities


def _cleanup_proof(value: object, session_id: str) -> bool:
    if not isinstance(value, dict):
        return False
    verified = value.get("lastVerifiedAt")
    requested = value.get("requestedAt")
    if not isinstance(verified, str) or not isinstance(requested, str):
        return False
    try:
        verified_at = datetime.fromisoformat(verified.replace("Z", "+00:00"))
        requested_at = datetime.fromisoformat(requested.replace("Z", "+00:00"))
    except ValueError:
        return False
    if verified_at.tzinfo is None or requested_at.tzinfo is None or verified_at < requested_at:
        return False
    return (
        value.get("sessionId") == session_id and value.get("state") == "cleanup_verified"
        and value.get("phase") == "complete"
        and all(value.get(key) is True for key in ("messagesVerified", "documentsVerified", "attachmentsVerified"))
        and value.get("pendingUploads") == [] and value.get("pendingUploadsTruncated") is False
        and value.get("scope") == "conversation_content_and_inline_originals"
        and value.get("backupsErased") is False and value.get("coordinationRetained") is True
        and value.get("autonomousCleanup") is False
    )


def cleanup(client: ApiClient, session_id: str) -> bool:
    root = f"/api/sessions/{session_id}"
    try:
        # No replay of the delete. Even an ambiguous response can be followed
        # by this exact owner's status read, not a global or orphan scan.
        client.request("DELETE", root, cleanup=True)
    except LiveError as exc:
        if exc.code not in ("http", "transport", "timeout", "shape"):
            return False
    for attempt in range(MAX_RECONCILES + 1):
        try:
            result = client.request("GET", f"{root}/deletion", cleanup=True)
            if result.status != 200:
                return False
            value = object_value(result.json())
            if _cleanup_proof(value, session_id):
                return True
            if (
                value.get("sessionId") != session_id
                or value.get("state") not in ("pending", "retryable")
                or attempt == MAX_RECONCILES
            ):
                return False
            resumed = client.request("POST", f"{root}/deletion/reconcile", cleanup=True)
            if resumed.status not in (200, 202):
                return False
        except (LiveError, EvaluationError):
            return False
    return False


def lifecycle_control(client: ApiClient, config: LiveConfig) -> None:
    created = object_value(_expect(client, "POST", "/api/sessions", {
        "title": "authored synthetic lifecycle control", "model": config.model_id,
        "libraryDocumentIds": [],
    }, status=201))
    session_id = client.created(created.get("id"))
    # The enrollment version is intentionally not exposed on Session responses.
    # An empty, newly owned fixture proves the cleanup path without a model call.
    if not cleanup(client, session_id):
        raise LiveError("cleanup")


def execute_live_case(
    case: LiveCase, config: LiveConfig, client: ApiClient, prices: dict,
    capabilities: ExecutionCapabilities,
) -> tuple[LiveResult, FailureCode | None]:
    session_id = None
    result = unknown_live(case)
    failure: FailureCode | None = None
    before = client.budget.http_attempts
    verified = False
    try:
        created = object_value(_expect(client, "POST", "/api/sessions", {
            "title": "authored synthetic evaluation", "model": config.model_id,
            "systemPrompt": case.system_prompt, "agentName": None,
            "libraryDocumentIds": [], "toolOverrides": {"added": [], "removed": []},
        }, status=201))
        session_id = client.created(created.get("id"))
        if (
            created.get("model") != config.model_id
            or created.get("agentName") is not None or created.get("libraryDocumentIds") not in (None, [])
        ):
            raise LiveError("capability")
        started = client.budget.clock()
        answer = object_value(_expect(client, "POST", "/api/chat", {
            "sessionId": session_id, "content": case.input, "model": config.model_id,
            "region": capabilities.region,
            "stream": False, "params": {"max_tokens": MAX_OUTPUT_TOKENS},
            **REQUEST_CONTROLS,
        }))
        elapsed = int((client.budget.clock() - started) * 1000)
        message = object_value(answer.get("message"))
        messages = _expect(client, "GET", f"/api/sessions/{session_id}/messages")
        if not isinstance(messages, list) or len(messages) != 2 or not isinstance(message.get("id"), str):
            raise LiveError("shape")
        persisted = [
            row for row in messages if isinstance(row, dict)
            and row.get("id") == message["id"] and row.get("role") == "assistant"
        ]
        if len(persisted) != 1 or persisted[0] != message:
            raise LiveError("shape")
        result = score_live(case, persisted[0], model_id=config.model_id, prices=prices, latency_ms=elapsed)
        if any(item.status != "passed" for item in result.checks if item.id in ("execution", "transport", "isolation", "cost")):
            failure = "shape"
    except LiveError as exc:
        failure = exc.code
    except (EvaluationError, ValidationError):
        failure = "shape"
    finally:
        if session_id is not None:
            verified = cleanup(client, session_id)
    # A create with an ambiguous/malformed response cannot be cleaned by guessing
    # an id. Keep it incomplete and stop before creating any further fixture.
    result = with_cleanup(result, verified=verified, requests=client.budget.http_attempts - before)
    return result, "cleanup" if not verified else failure


def evaluate_live(
    dataset: LiveDataset, identity: LiveIdentity, config: LiveConfig, client: ApiClient,
    *, operation: Literal["run", "preflight"] = "run",
) -> LiveReport:
    rows = [unknown_live(case) for case in dataset.cases]
    _, prices = source_documents()
    termination = "complete"
    lifecycle = LifecycleObservation()
    control_start = None
    api_preflight = "unknown"
    try:
        observed, capabilities = preflight_api(client, config)
        identity = LiveIdentity(**{
            **identity.model_dump(), "advertised_catalog_sha256": observed,
            "execution_capabilities_version": capabilities.version,
            "request_reductions_version": capabilities.reductionControlsVersion,
        })
        api_preflight = "passed"
        if operation == "preflight":
            return make_live_report(
                identity, rows, client.budget, "not_run", operation=operation, api_preflight=api_preflight,
            )
        client.budget.require_work(1)
        control_start = client.budget.http_attempts
        lifecycle = LifecycleObservation(status="unknown")
        lifecycle_control(client, config)
        lifecycle = LifecycleObservation(
            status="passed", http_attempts=client.budget.http_attempts - control_start,
        )
        for index, case in enumerate(dataset.cases):
            client.budget.require_work(3)
            rows[index], failure = execute_live_case(case, config, client, prices, capabilities)
            if failure is not None:
                termination = failure
                break
    except LiveError as exc:
        termination = exc.code
    except (EvaluationError, ValidationError):
        termination = "shape"
    if lifecycle.status == "unknown" and control_start is not None:
        lifecycle = LifecycleObservation(
            status="unknown", http_attempts=client.budget.http_attempts - control_start,
        )
    return make_live_report(
        identity, rows, client.budget, termination, lifecycle,
        operation=operation, api_preflight=api_preflight,
    )


def worker() -> None:
    logging.disable(logging.CRITICAL)
    config = LiveConfig.model_validate_json(os.environ.pop(_WORKER_CONFIG_ENV))
    token = os.environ.pop(TOKEN_ENV)
    source_revision = os.environ.pop(_REVISION_ENV)
    operation = os.environ.pop(_WORKER_OPERATION_ENV)
    if operation not in ("run", "preflight"):
        raise LiveError("configuration")
    bind_token(config, token)
    dataset = load_live_dataset()
    identity = build_identity(dataset, source_revision, config)
    budget = Budget()
    transport = HTTPS(config, token, budget)
    report = evaluate_live(
        dataset, identity, config, ApiClient(transport, budget, accounts_bytes=True), operation=operation,
    )
    sys.stdout.buffer.write(canonical_bytes(report.model_dump(mode="json")))


def run_worker(
    dataset: LiveDataset, identity: LiveIdentity, config: LiveConfig, token: str,
    *, operation: Literal["run", "preflight"] = "run",
) -> LiveReport:
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    environment.update({
        _WORKER_CONFIG_ENV: config.model_dump_json(), TOKEN_ENV: token,
        _REVISION_ENV: identity.source_revision,
        _WORKER_OPERATION_ENV: operation,
    })
    bootstrap = "import sys; sys.path.insert(0, sys.argv[1]); from scripts.evaluations.live import worker; worker()"
    data = bytearray()
    pipe_errors: list[OSError] = []
    failure: FailureCode = "worker"
    try:
        with subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", bootstrap, str(ROOT)], cwd=ROOT, env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ) as process:
            assert process.stdout is not None
            output = process.stdout

            def drain() -> None:
                try:
                    while len(data) <= MAX_LIVE_REPORT_BYTES:
                        chunk = output.read(min(8192, MAX_LIVE_REPORT_BYTES + 1 - len(data)))
                        if not chunk:
                            return
                        data.extend(chunk)
                    process.kill()
                except OSError as exc:
                    pipe_errors.append(exc)

            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            try:
                code = process.wait(timeout=MAX_RUN_SECONDS + 15)
            except subprocess.TimeoutExpired:
                failure = "timeout"
                process.kill()
                code = process.wait(timeout=5)
            reader.join(timeout=5)
            if (
                failure == "timeout" or code != 0 or reader.is_alive() or pipe_errors
                or not data or len(data) > MAX_LIVE_REPORT_BYTES
            ):
                raise LiveError(failure)
        report = LiveReport.model_validate(decode_json(bytes(data), MAX_LIVE_REPORT_BYTES, "worker_output_too_large"))
        observation_fields = {
            "advertised_catalog_sha256", "execution_capabilities_version", "request_reductions_version",
        }
        observed = report.identity.model_dump(exclude=observation_fields)
        if observed != identity.model_dump(exclude=observation_fields) or report.operation != operation:
            raise LiveError("worker")
        return report
    except (OSError, subprocess.SubprocessError, EvaluationError, ValidationError):
        return make_live_report(
            identity, [unknown_live(case) for case in dataset.cases], None, failure,
            LifecycleObservation(status="unknown" if operation == "run" else "not_run"),
            operation=operation, api_preflight="unknown",
        )


def write_live_report(report: LiveReport, path: Path) -> None:
    validated = LiveReport.model_validate(report.model_dump(mode="json"))
    dataset = load_live_dataset()
    if (
        validated.identity.case_ids != tuple(case.id for case in dataset.cases)
        or validated.identity.dataset_sha256 != digest(dataset.model_dump(mode="json"))
    ):
        raise LiveError("shape")
    payload = canonical_bytes(validated.model_dump(mode="json")) + b"\n"
    if len(payload) > MAX_LIVE_REPORT_BYTES:
        raise LiveError("bounds")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "run"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--judge", default="disabled")
    parser.add_argument("--production-content", action="store_true")
    args = parser.parse_args(argv)
    operation: Literal["run", "preflight"] = "preflight" if args.command == "preflight" else "run"
    if args.judge != "disabled" or args.production_content:
        print("evaluation_refused: judges and production-content input are disabled.", file=sys.stderr)
        return 2
    try:
        dataset = load_live_dataset()
        identity = build_identity(dataset, revision())
        try:
            config = config_from_environment()
            identity = build_identity(dataset, identity.source_revision, config)
            token = os.environ.get(TOKEN_ENV, "")
            bind_token(config, token)
        except LiveError as exc:
            report = make_live_report(
                identity, [unknown_live(case) for case in dataset.cases], Budget(), exc.code,
                operation=operation,
            )
        except ValidationError:
            report = make_live_report(
                identity, [unknown_live(case) for case in dataset.cases], Budget(), "configuration",
                operation=operation,
            )
        else:
            report = run_worker(dataset, identity, config, token, operation=operation)
        if args.output is not None:
            write_live_report(report, args.output)
            print(f"live-authored-synthetic: {report.coverage.passed}/{report.coverage.total} passed; gate={report.gate}; termination={report.termination}")
        else:
            print(canonical_bytes(report.model_dump(mode="json")).decode("ascii"))
        if operation == "preflight":
            return 0 if report.api_preflight == "passed" else 2
        return 0 if report.gate == "passed" else 1 if report.gate == "failed" else 2
    except (EvaluationError, ValidationError, OSError, ImportError, subprocess.SubprocessError, UnicodeError):
        print("evaluation_refused: unavailable source identity, invalid input or output.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
