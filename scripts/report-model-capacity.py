#!/usr/bin/env python3
"""Collect bounded allocation, quota and aggregate usage evidence without Azure writes.

Requires an existing Azure CLI login, an explicit subscription, resource group
and azd environment. Does not select a subscription, invoke models, infer pools,
recommend capacity changes or call the maximum-profile planner.

Exit 0: complete requested observations under consistent operator pool assertions.
Exit 2: partial/unknown evidence, invalid input, or failed collection/output.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from _capacity_evidence import (
    ROOT,
    AzureReader,
    EvidenceError,
    Scope,
    Window,
    collect,
    load_catalog,
    render,
    subscription_id,
)


class Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.exit(2, "capacity-evidence: invalid_arguments (use --help)\n")


def main(argv: list[str] | None = None) -> int:
    parser = Parser(description=__doc__)
    parser.add_argument("--subscription", required=True, help="Expected subscription GUID; every Azure read is scoped to it.")
    parser.add_argument("--resource-group", required=True, help="Exact azd-owned resource group, not a name search.")
    parser.add_argument("--environment-name", required=True, help="Expected azd environment and ownership tags.")
    parser.add_argument("--days", type=int, choices=range(1, 8), default=1, help="Whole 24-hour days to observe (1-7).")
    parser.add_argument("--end", help="UTC whole-hour end, at least one closed hour behind now and at most 24 hours older.")
    parser.add_argument("--pool-evidence", type=Path, help="Optional fresh, subscription-bound operator pool assertions (JSON).")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    parser.add_argument("--output", type=Path, help="Also write this format to a NEW local file; parent directory must exist.")
    args = parser.parse_args(argv)
    try:
        if args.output is not None and (args.output.exists() or args.output.is_symlink()):
            raise EvidenceError("output_already_exists")
        catalog = load_catalog(ROOT / "infra" / "models.json")
        scope = Scope(subscription_id(args.subscription), args.resource_group, args.environment_name, catalog)
        window = Window.create(args.days, args.end, datetime.now(UTC))
        report = collect(scope, window, AzureReader(scope), args.pool_evidence)
        text = render(report, args.format)
        if args.output is not None:
            try:
                with args.output.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(text)
            except FileExistsError:
                raise EvidenceError("output_already_exists") from None
            except OSError:
                raise EvidenceError("output_unavailable") from None
        sys.stdout.write(text)
        return 0 if report["status"] == "complete" else 2
    except EvidenceError as exc:
        # No command, subscription ID, file path, provider message, stdout or
        # exception text is included in this small failure report.
        failure = {"schemaVersion": 1, "status": "unknown", "error": exc.code, "writes": "none", "recommendations": []}
        print(json.dumps(failure) if args.format == "json" else f"Capacity evidence: unknown ({exc.code}); no Azure writes.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
