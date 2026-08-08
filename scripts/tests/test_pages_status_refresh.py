"""The published status snapshot must target the real deployment and fail closed.

The Pages workflow used to pass only a subscription to status-snapshot.ps1. The
script requires a resource group, and the Pages job has no selected azd
environment from which to discover one. Both login and refresh were
``continue-on-error``, so the workflow silently republished a 2026-08-01 seed
that still listed the PostgreSQL server deleted six days later.
"""
from __future__ import annotations

import json
import subprocess
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
SERVICES_HTML = REPO / "site" / "services.html"


class PagesStatusRefreshTests(unittest.TestCase):
    def _steps(self) -> list[dict]:
        document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        return document["jobs"]["build"]["steps"]

    def _render_services(self, generated_at: str) -> dict[str, str]:
        app_path = json.dumps(str(APP_JS))
        generated = json.dumps(generated_at)
        harness = f"""
        Date.now = () => Date.parse("2026-08-08T12:00:00Z");
        const nodes = {{
          "services-root": {{ innerHTML: "" }},
          "services-updated": {{ innerHTML: "" }}
        }};
        global.window = {{
          AI4IA_SERVICES: [{{
            name: "Storage", icon: "S", group: "Data",
            azureType: "Microsoft.Storage/storageAccounts",
            resourcePattern: "st-*", summary: "Stores files.",
            module: "data.bicep", identity: "Managed identity", docs: []
          }}],
          AI4IA_INVENTORY: {{
            generatedAt: {generated},
            resources: [{{ type: "microsoft.storage/storageaccounts", name: "st-one" }}]
          }},
          matchMedia: () => ({{ matches: false }})
        }};
        global.document = {{
          getElementById: (id) => nodes[id] || null,
          addEventListener: (name, callback) => callback(),
          querySelector: () => null,
          createElement: () => ({{ innerHTML: "", content: null }})
        }};
        require({app_path});
        process.stdout.write(JSON.stringify({{
          cards: nodes["services-root"].innerHTML,
          freshness: nodes["services-updated"].innerHTML
        }}));
        """
        result = subprocess.run(
            ["node", "-e", harness],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

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
            for path in (STATUS_HTML, INDEX_HTML, SERVICES_HTML)
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
        self.assertIn("var freshness = snapshotFreshness(generatedAt)", app)
        self.assertIn('renderSnapshotFreshness(el("updated"), s.generatedAt)', app)
        self.assertIn("stateBadge(freshness.state, freshness.label)", app)
        self.assertIn("older than 24 hours is visibly marked stale", status.lower())
        self.assertIn('id="updated" aria-live="polite"', status)

    def test_services_counts_use_the_shared_snapshot_freshness_state(self) -> None:
        current = self._render_services("2026-08-08T11:00:00Z")
        stale = self._render_services("2026-08-07T11:00:00Z")

        self.assertIn("1 in current snapshot", current["cards"])
        self.assertIn("current snapshot", current["freshness"])
        self.assertIn("1 in stale snapshot", stale["cards"])
        self.assertIn("stale snapshot", stale["freshness"])
        self.assertNotIn(" live", current["cards"].lower())
        self.assertNotIn(" live", stale["cards"].lower())

    def test_services_copy_describes_timestamped_evidence_not_live_counts(self) -> None:
        services = SERVICES_HTML.read_text(encoding="utf-8").lower()
        self.assertIn("count badges come from the timestamped", services)
        self.assertIn("whether that evidence is current or stale", services)
        self.assertNotIn("live badge", services)
        self.assertIn('id="services-updated" aria-live="polite"', services)


if __name__ == "__main__":
    unittest.main()
