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
APP_JS = REPO / "site" / "assets" / "app.js"
STATUS_HTML = REPO / "site" / "status.html"
INDEX_HTML = REPO / "site" / "index.html"
DEPLOYMENT_DOC = REPO / "docs" / "runbooks" / "deployment.md"
META_JS = REPO / "site" / "data" / "meta.js"


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

    def test_azure_login_and_refresh_are_mandatory(self) -> None:
        login = next(
            step
            for step in self._steps()
            if step.get("uses", "").startswith("azure/login@")
        )
        refresh = next(
            step
            for step in self._steps()
            if step.get("name") == "Refresh deployment status snapshot"
        )
        for step in (login, refresh):
            self.assertNotIn("if", step)
            self.assertNotIn("continue-on-error", step)

    def test_missing_repository_variables_fail_before_pages_upload(self) -> None:
        steps = self._steps()
        required = next(
            step
            for step in steps
            if step.get("name") == "Require status snapshot credentials"
        )
        login_index = next(
            i
            for i, step in enumerate(steps)
            if step.get("uses", "").startswith("azure/login@")
        )
        upload_index = next(
            i
            for i, step in enumerate(steps)
            if step.get("uses", "").startswith("actions/upload-pages-artifact@")
        )
        self.assertLess(steps.index(required), login_index)
        self.assertLess(steps.index(required), upload_index)
        for name in (
            "AZURE_CLIENT_ID",
            "AZURE_TENANT_ID",
            "AZURE_SUBSCRIPTION_ID",
            "AZURE_ENV_NAME",
        ):
            self.assertIn(name, required["env"])
            self.assertIn(name, required["run"])
        self.assertIn("exit 1", required["run"])

    def test_main_branch_federation_is_required_on_the_deploy_identity(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        deployment = DEPLOYMENT_DOC.read_text(encoding="utf-8")
        subject = "repo:ian-t-adams/AI4IA:ref:refs/heads/main"
        self.assertIn(subject, workflow)
        self.assertIn(subject, deployment)
        self.assertIn(
            "Both subjects are required on the same deployment identity", deployment
        )
        self.assertIn("Do not create", deployment)
        self.assertIn("a second Azure identity for Pages", deployment)
        self.assertNotIn("if you also want pushes", deployment)

    def test_public_copy_calls_status_a_timestamped_snapshot_not_live_health(self) -> None:
        public_copy = "\n".join(
            path.read_text(encoding="utf-8").lower()
            for path in (STATUS_HTML, INDEX_HTML)
        )
        self.assertIn("static snapshot", public_copy)
        self.assertIn("not a live poll", public_copy)
        self.assertNotIn("live health", public_copy)
        self.assertNotIn("live status", public_copy)

    def test_portal_audience_matches_the_enterprise_product_positioning(self) -> None:
        audience_copy = "\n".join(
            path.read_text(encoding="utf-8").lower()
            for path in (INDEX_HTML, META_JS)
        )
        self.assertIn("enterprise knowledge work", audience_copy)
        self.assertIn("azure capabilities", audience_copy)
        self.assertNotIn("personal use", audience_copy)
        self.assertNotIn("customer demos", audience_copy)

    def test_status_marks_snapshots_stale_after_24_hours(self) -> None:
        app = APP_JS.read_text(encoding="utf-8")
        status = STATUS_HTML.read_text(encoding="utf-8")
        self.assertIn("var STATUS_STALE_AFTER_HOURS = 24;", app)
        self.assertIn("ageHours > STATUS_STALE_AFTER_HOURS", app)
        self.assertIn('label: "stale snapshot"', app)
        self.assertIn('label: "current snapshot"', app)
        self.assertIn("snapshotFreshness(s.generatedAt)", app)
        self.assertIn("stateBadge(freshness.state, freshness.label)", app)
        self.assertIn("older than 24 hours is visibly marked stale", status.lower())
        self.assertIn('id="updated" aria-live="polite"', status)


if __name__ == "__main__":
    unittest.main()
