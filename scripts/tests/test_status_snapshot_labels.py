"""Portal inventory labels must cover live, user-facing service cards."""
from __future__ import annotations

import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SNAPSHOT = REPO / "scripts" / "status-snapshot.ps1"
SERVICES = REPO / "site" / "data" / "services.js"


class StatusSnapshotLabelTests(unittest.TestCase):
    def test_durable_task_is_named_and_grouped_consistently(self) -> None:
        snapshot = SNAPSHOT.read_text(encoding="utf-8")
        services = SERVICES.read_text(encoding="utf-8")

        self.assertIn(
            "'microsoft.durabletask/schedulers'",
            snapshot,
            "a live scheduler must not render as an unknown 'Other' resource",
        )
        self.assertIn("label = 'Durable Task Scheduler'", snapshot)
        self.assertIn(
            'azureType: "Microsoft.DurableTask/schedulers"',
            services,
            "the Services page claims to catalog every deployed Azure service",
        )
        self.assertIn('name: "Durable Task Scheduler"', services)


if __name__ == "__main__":
    unittest.main()
