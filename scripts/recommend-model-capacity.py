#!/usr/bin/env python3
"""Recommend production capacities offline from a saved bounded evidence report.

Reads infra/models.json and --report only. Policy, pool identities, criticality,
reserves and sizing assumptions must be explicitly configured by an operator.
Prints to stdout; there is no apply, output-file, Azure, or catalog-write mode.
Exit 0: complete recommendations for review. Exit 2: unknown/partial or invalid.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from _capacity_evidence import ROOT, EvidenceError
from _capacity_recommendations import load_policy, load_snapshot, recommend, render
from _production_capacity import bind_scope

CATALOG = ROOT / "infra" / "models.json"


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.exit(2, "capacity-recommendations: invalid_arguments (use --help)\n")


def main(argv: list[str] | None = None) -> int:
    parser = Parser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True, help="A fresh JSON report from report-model-capacity.py.")
    parser.add_argument("--subscription", required=True, help="Expected subscription GUID; never selects or logs in to Azure.")
    parser.add_argument("--resource-group", required=True, help="Expected exact resource group.")
    parser.add_argument("--environment-name", required=True, help="Expected azd environment.")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    args = parser.parse_args(argv)
    try:
        policy = load_policy(CATALOG)
        bind_scope(policy, args.subscription, args.resource_group, args.environment_name)
        now = datetime.now(UTC)
        snapshot = load_snapshot(args.report, policy, now)
        report = recommend(policy, snapshot, now)
        sys.stdout.write(render(report, args.format))
        return 0 if report["status"] == "complete" else 2
    except EvidenceError as exc:
        failure = {"schemaVersion": 1, "status": "unknown", "error": exc.code, "writes": "none", "azureCalls": 0}
        print(json.dumps(failure) if args.format == "json" else f"Capacity recommendations: unknown ({exc.code}); no writes.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
