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
GREENFIELD_DOC = REPO / "docs" / "runbooks" / "greenfield-standup.md"
STATUS_SCRIPT = REPO / "scripts" / "status-snapshot.ps1"
META_JS = REPO / "site" / "data" / "meta.js"
SERVICES_HTML = REPO / "site" / "services.html"
DOCS_HTML = REPO / "site" / "docs.html"
REQUIREMENTS_HTML = REPO / "site" / "requirements.html"
ARCHITECTURE_HTML = REPO / "site" / "architecture.html"
NOT_FOUND_HTML = REPO / "site" / "404.html"


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

    def _render_status(
        self,
        *,
        health_source: dict | None = None,
        resources: list[dict] | None = None,
    ) -> dict[str, str]:
        app_path = json.dumps(str(APP_JS))
        health_source_json = json.dumps(
            health_source
            or {
                "status": "available",
                "providerState": "Registered",
                "records": 0,
                "note": "Resource Health query succeeded.",
            }
        )
        resources_json = json.dumps(
            resources
            or [
                {
                    "name": "st-one",
                    "label": "Storage",
                    "group": "Data",
                    "location": "eastus",
                    "provisioningState": "Succeeded",
                    "availability": "Unknown",
                    "state": "provisioned",
                }
            ]
        )
        harness = f"""
        const nodes = {{
          "status-stats": {{ innerHTML: "" }},
          "status-resource-group": {{ textContent: "" }},
          "health-source": {{ innerHTML: "" }},
          "resources-body": {{ innerHTML: "" }},
          "endpoints": {{ innerHTML: "" }},
          "updated": {{ innerHTML: "" }}
        }};
        global.window = {{
          AI4IA_STATUS: {{
            generatedAt: "2026-08-08T11:00:00Z",
            resourceGroup: "rg-test",
            summary: {{ total: 1, endpointsUp: 0, endpointsTot: 0 }},
            endpoints: [],
            healthSource: {health_source_json}
          }},
          AI4IA_INVENTORY: {{
            resources: {resources_json}
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
          resources: nodes["resources-body"].innerHTML,
          stats: nodes["status-stats"].innerHTML,
          healthSource: nodes["health-source"].innerHTML
        }}));
        """
        result = subprocess.run(
            ["node", "-e", harness],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    def _render_without_data(self) -> dict[str, str]:
        app_path = json.dumps(str(APP_JS))
        harness = f"""
        const ids = [
          "features", "stack", "regions", "envfacts",
          "status-stats", "status-resource-group", "resources-body",
          "health-source", "endpoints", "updated", "services-root", "services-updated",
          "modules", "iac-meta", "rbac", "packages", "prereqs", "docs-root"
        ];
        const nodes = Object.fromEntries(ids.map((id) => [
          id,
          {{ innerHTML: "", textContent: "" }}
        ]));
        global.window = {{ matchMedia: () => ({{ matches: false }}) }};
        global.document = {{
          getElementById: (id) => nodes[id] || null,
          addEventListener: (name, callback) => callback(),
          querySelector: () => null,
          createElement: () => ({{ innerHTML: "", content: null }})
        }};
        require({app_path});
        process.stdout.write(JSON.stringify(Object.fromEntries(
          ids.map((id) => [id, nodes[id].innerHTML || nodes[id].textContent])
        )));
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
        self.assertIn("-OutDir $outDir", run)
        self.assertIn("LastWriteTimeUtc", run)
        self.assertNotIn("${{ vars.", run)
        self.assertNotIn("continue-on-error", refresh)

    def test_status_script_default_output_path_is_cross_platform(self) -> None:
        script = STATUS_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("[System.IO.Path]::Combine(", script)
        self.assertNotIn("'..\\site\\data'", script)

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
        greenfield = GREENFIELD_DOC.read_text(encoding="utf-8")
        subject = "repo:<owner>/<repo>:ref:refs/heads/main"
        self.assertIn(subject, workflow)
        self.assertIn(subject, greenfield)
        concrete_subject = r"repo:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+:ref:refs/heads/main"
        self.assertNotRegex(workflow, concrete_subject)
        self.assertNotRegex(greenfield, concrete_subject)
        self.assertIn(
            "Both subjects are required on the same deployment identity", greenfield
        )
        self.assertIn("Do not create", greenfield)
        self.assertIn("a second Azure identity for Pages", greenfield)
        self.assertIn("refuses to publish stale seed data", greenfield)
        self.assertNotIn("if you also want pushes", greenfield)

    def test_public_copy_calls_status_a_timestamped_snapshot_not_live_health(self) -> None:
        public_copy = "\n".join(
            path.read_text(encoding="utf-8").lower()
            for path in (STATUS_HTML, INDEX_HTML, SERVICES_HTML)
        )
        self.assertIn("static snapshot", public_copy)
        self.assertIn("not a live poll", public_copy)
        self.assertNotIn("live health", public_copy)
        self.assertNotIn("live status", public_copy)

    def test_status_page_renders_the_snapshot_resource_group(self) -> None:
        status = STATUS_HTML.read_text(encoding="utf-8")
        app = APP_JS.read_text(encoding="utf-8")
        self.assertIn('id="status-resource-group"', status)
        self.assertNotIn("rg-ai4ia-slurmfactory", status)
        self.assertIn("resourceGroup.textContent = s.resourceGroup", app)

    def test_status_uses_shared_inventory_without_duplicating_resources(self) -> None:
        rendered = self._render_status()

        self.assertIn("Storage", rendered["resources"])
        self.assertIn("st-one", rendered["resources"])
        self.assertIn("1", rendered["stats"])

    def test_status_surfaces_a_resource_health_source_outage(self) -> None:
        rendered = self._render_status(
            health_source={
                "status": "unavailable",
                "providerState": "NotRegistered",
                "records": 0,
                "note": "Microsoft.ResourceHealth is not registered.",
            }
        )

        self.assertIn('id="health-source"', STATUS_HTML.read_text(encoding="utf-8"))
        self.assertIn("Resource Health source", rendered["stats"])
        self.assertIn("Unavailable", rendered["stats"])
        self.assertIn("health not checked", rendered["stats"])
        self.assertIn("Resource Health unavailable", rendered["healthSource"])
        self.assertIn("not registered", rendered["healthSource"])
        self.assertIn("Not checked", rendered["resources"])

    def test_status_derives_degraded_from_resource_health_availability(self) -> None:
        rendered = self._render_status(
            resources=[
                {
                    "name": "st-one",
                    "label": "Storage",
                    "group": "Data",
                    "location": "eastus",
                    "provisioningState": "Succeeded",
                    "availability": "Degraded",
                    "state": "provisioned",
                }
            ]
        )

        self.assertIn("degraded", rendered["resources"])
        self.assertIn("Degraded", rendered["resources"])

    def test_missing_portal_data_renders_recovery_instead_of_staying_blank(self) -> None:
        rendered = self._render_without_data()

        for host in (
            "features",
            "stack",
            "regions",
            "envfacts",
            "status-stats",
            "health-source",
            "resources-body",
            "endpoints",
            "updated",
            "services-root",
            "services-updated",
            "modules",
            "iac-meta",
            "rbac",
            "packages",
            "prereqs",
            "docs-root",
        ):
            with self.subTest(host=host):
                self.assertIn("unavailable", rendered[host].lower())
        self.assertIn("portal publishing workflow", rendered["features"])

    def test_every_portal_page_has_a_no_javascript_recovery_message(self) -> None:
        for page in (
            INDEX_HTML,
            STATUS_HTML,
            SERVICES_HTML,
            DOCS_HTML,
            REQUIREMENTS_HTML,
            ARCHITECTURE_HTML,
        ):
            with self.subTest(page=page.name):
                html = page.read_text(encoding="utf-8")
                self.assertIn("<noscript>", html)
                self.assertIn("JavaScript is unavailable.", html)

    def test_portal_has_a_branded_recovery_page_for_stale_links(self) -> None:
        html = NOT_FOUND_HTML.read_text(encoding="utf-8")

        self.assertIn("<h1>Page not found</h1>", html)
        self.assertIn(
            'href="https://ian-t-adams.github.io/AI4IA/"',
            html,
        )
        self.assertIn(
            'href="https://ian-t-adams.github.io/AI4IA/docs.html"',
            html,
        )
        self.assertIn('aria-label="Primary"', html)

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
