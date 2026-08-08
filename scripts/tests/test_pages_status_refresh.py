"""The published status snapshot must target the real deployment and fail closed.

The Pages workflow used to pass only a subscription to status-snapshot.ps1. The
script requires a resource group, and the Pages job has no selected azd
environment from which to discover one. Both login and refresh were
``continue-on-error``, so the workflow silently republished a 2026-08-01 seed
that still listed the PostgreSQL server deleted six days later.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "pages.yml"


class PagesStatusRefreshTests(unittest.TestCase):
    def _steps(self) -> list[dict]:
        document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        return document["jobs"]["build"]["steps"]

    def test_refresh_passes_the_resource_group_and_endpoint_urls(self) -> None:
        refresh = next(
            step for step in self._steps() if step.get("name") == "Refresh deployment status snapshot"
        )
        run = refresh["run"]
        self.assertIn("-ResourceGroup $resourceGroup", run)
        self.assertIn("-WebUrl $webUrl", run)
        self.assertIn("-ProxyUrl $proxyUrl", run)
        self.assertNotIn("continue-on-error", refresh)

    def test_configured_azure_login_does_not_fail_open(self) -> None:
        login = next(step for step in self._steps() if step.get("uses", "").startswith("azure/login@"))
        self.assertNotIn(
            "continue-on-error",
            login,
            "publishing an old seed as current health is worse than failing the Pages refresh",
        )


if __name__ == "__main__":
    unittest.main()
