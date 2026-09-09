"""Run with python -m scripts.evaluations from the repository root."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from pydantic import ValidationError

from .contracts import (
    MAX_REPORT_BYTES, EvaluationError, canonical_bytes, decode_json, digest, load_dataset,
)


def worker() -> None:
    from .offline import execute_case

    request = decode_json(sys.stdin.buffer.read(1025), 1024, "worker_request_too_large")
    if not isinstance(request, dict) or set(request) != {"case_id", "dataset_sha256"}:
        raise EvaluationError("invalid_worker_request")
    dataset = load_dataset()
    if request["dataset_sha256"] != digest(dataset.model_dump(mode="json")):
        raise EvaluationError("worker_dataset_changed")
    case = next((case for case in dataset.cases if case.id == request["case_id"]), None)
    if case is None:
        raise EvaluationError("unknown_case")
    result = execute_case(dataset, case)
    payload = canonical_bytes(result.model_dump(mode="json"))
    if len(payload) > 16_384:
        raise EvaluationError("worker_output_too_large")
    sys.stdout.buffer.write(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run every committed synthetic case, with no real providers.")
    run.add_argument("--output", type=Path, help="New local report file; never overwritten.")
    run.add_argument("--mode", default="offline")
    run.add_argument("--judge", default="disabled")
    run.add_argument("--production-content", action="store_true")
    compare = sub.add_parser("compare", help="Compare fully identified, compatible reports.")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)
    args = parser.parse_args(argv)
    if args.command == "run" and (
        args.mode != "offline" or args.judge != "disabled" or args.production_content
    ):
        print("approval_required: live, judge, and production-content evaluation are disabled.", file=sys.stderr)
        return 2
    try:
        from .runner import compare_reports, evaluate, read_report, write_report

        if args.command == "compare":
            comparison = compare_reports(read_report(args.baseline), read_report(args.candidate))
            print(comparison.model_dump_json())
            return 1 if comparison.regressed else 2 if comparison.unknown else 0
        report = evaluate(load_dataset())
        payload = canonical_bytes(report.model_dump(mode="json"))
        if len(payload) > MAX_REPORT_BYTES:
            raise EvaluationError("report_too_large")
        if args.output:
            write_report(report, args.output)
            print(
                f"offline-synthetic: {report.coverage.passed}/{report.coverage.total} passed; "
                f"{report.coverage.failed} failed; {report.coverage.unknown} unknown; "
                f"{report.coverage.unscored} unscored"
            )
        else:
            print(payload.decode("utf-8"))
        return 0 if report.gate == "passed" else 1 if report.gate == "failed" else 2
    except (EvaluationError, ValidationError, OSError, ImportError, subprocess.SubprocessError):
        # Do not echo paths, invalid JSON inputs, or exception payloads. Refusal
        # is visibly nonzero; worker crashes already have retained unknown rows.
        print("evaluation_refused: invalid input, incompatible versions, or unavailable output.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
