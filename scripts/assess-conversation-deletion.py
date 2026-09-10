#!/usr/bin/env python3
"""Read-only, explicit-cohort deletion observations; never cleanup or approval.

Only `collect` constructs an Azure reader. `rehearse`, `check`, imports and help
are offline. Exit 0 means complete requested metadata (or a valid saved report),
not rollout readiness. Exit 2 means unknown, partial, refused or malformed.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from _deletion_assessment import (
    MAX_FIXTURE_BYTES,
    MAX_INPUT_BYTES,
    MAX_REPORT_BYTES,
    AssessmentError,
    FixtureReader,
    Scope,
    collect,
    exact_fields,
    obj,
    read_json,
    reference_evidence,
    render,
    validate_report,
)


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.exit(2, "deletion-assessment: invalid_arguments (use --help)\n")


def main(argv: list[str] | None = None) -> int:
    parser = Parser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("collect", help="Explicitly read an approved private cohort; no write operations.", allow_abbrev=False)
    live.add_argument("--cohort", type=Path, required=True, help="Private exact account/database/owner+session JSON.")
    live.add_argument("--references", type=Path, help="Optional private human references; never fetched or treated as approval.")
    live.add_argument("--output", type=Path, required=True, help="NEW local report file. Never overwritten.")
    rehearsal = commands.add_parser("rehearse", help="Run the same collector on bounded synthetic metadata, offline.", allow_abbrev=False)
    rehearsal.add_argument("--fixture", type=Path, required=True)
    rehearsal.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("check", help="Validate a saved report's schema, source, digest and coverage, offline.", allow_abbrev=False)
    check.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            report, _ = read_json(args.report, MAX_REPORT_BYTES)
            checked = validate_report(report)
            print(f"deletion-assessment: report_valid ({checked['status']})")
            return 0 if checked["status"] == "complete" else 2
        now = datetime.now(UTC)
        references, reference_sha256 = None, None
        fixture_reader: FixtureReader | None = None
        if args.command == "rehearse":
            fixture, input_sha256 = read_json(args.fixture, MAX_FIXTURE_BYTES)
            fixture = obj(fixture)
            exact_fields(fixture, {"schemaVersion", "synthetic", "scope", "observations"})
            if type(fixture["schemaVersion"]) is not int or fixture["schemaVersion"] != 1 or fixture["synthetic"] is not True:
                raise AssessmentError("invalid_fixture_version")
            scope = Scope.parse(fixture["scope"])
            fixture_reader = FixtureReader(scope, fixture["observations"])
        else:
            raw, input_sha256 = read_json(args.cohort, MAX_INPUT_BYTES)
            scope = Scope.parse(raw)
            if args.references:
                raw_references, reference_sha256 = read_json(args.references, MAX_INPUT_BYTES)
                references = reference_evidence(raw_references, scope, now)
        # Reserve before credential construction/collection, including symlink and
        # racing-file cases. A failed/partial file is never silently rewritten.
        with args.output.open("xb") as stream:
            if args.command == "collect":
                from _deletion_assessment_sdk import IsolatedReader

                with IsolatedReader(scope) as reader:
                    report = collect(
                        scope, reader, mode="live", input_sha256=input_sha256, now=now,
                        references=references, reference_sha256=reference_sha256,
                    )
            else:
                if fixture_reader is None:
                    raise AssessmentError("fixture_unavailable")
                report = collect(scope, fixture_reader, mode="synthetic", input_sha256=input_sha256, now=now)
            body = render(report)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        sys.stdout.write(body.decode("ascii"))
        return 0 if report["status"] == "complete" else 2
    except AssessmentError as exc:
        print(f"deletion-assessment: unknown ({exc.code})")
    except FileExistsError:
        print("deletion-assessment: unknown (output_exists)")
    except OSError:
        print("deletion-assessment: unknown (local_io_unavailable)")
    except ImportError:
        print("deletion-assessment: unknown (install_existing_api_dev_dependencies)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
