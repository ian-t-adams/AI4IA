"""The live quality schedule stays opt-in, separately authorized and non-PR-blocking."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.tests._platform import find_bash

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "live-evaluations.yml"


class LiveEvaluationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.activation = self.document["jobs"]["activation"]
        self.evaluate = self.document["jobs"]["evaluate"]

    def test_inert_schedule_is_never_a_required_pull_request_gate(self):
        events = self.document.get("on", self.document.get(True))
        self.assertEqual(set(events), {"workflow_dispatch", "schedule"})
        self.assertEqual(events["schedule"], [{"cron": "37 7 * * 1"}])
        self.assertEqual(self.document["permissions"], {})
        self.assertEqual(self.activation["permissions"], {})
        self.assertEqual(self.evaluate["permissions"], {"contents": "read", "id-token": "write"})
        self.assertEqual(self.evaluate["needs"], "activation")
        self.assertIn("needs.activation.outputs.enabled == 'true'", self.evaluate["if"])
        self.assertIn("github.ref == 'refs/heads/main'", self.evaluate["if"])
        self.assertNotIn("environment", self.evaluate)
        self.assertFalse(self.document["concurrency"]["cancel-in-progress"])
        self.assertFalse(self.evaluate.get("continue-on-error", False))

    def test_pinned_dedicated_login_has_no_subscription_or_deploy_credentials(self):
        actions = [step for step in self.evaluate["steps"] if "uses" in step]
        for step in actions:
            self.assertRegex(step["uses"], r"^[^@]+@[0-9a-f]{40}$")
        login = next(step for step in actions if step["uses"].startswith("azure/login@"))
        self.assertEqual(login["with"], {
            "client-id": "${{ vars.AI4IA_LIVE_EVAL_CLIENT_ID }}",
            "tenant-id": "${{ vars.AI4IA_LIVE_EVAL_TENANT_ID }}",
            "allow-no-subscriptions": True,
        })
        checkout = next(step for step in actions if step["uses"].startswith("actions/checkout@"))
        self.assertIs(checkout["with"]["persist-credentials"], False)
        env = self.evaluate["env"]
        self.assertEqual(env["AI4IA_LIVE_EVAL_DEPLOY_CLIENT_ID"], "${{ vars.AZURE_CLIENT_ID }}")
        self.assertEqual(env["AI4IA_LIVE_EVAL_ACTOR_OBJECT_ID"], "${{ vars.AI4IA_LIVE_EVAL_ACTOR_OBJECT_ID }}")
        self.assertEqual(login["env"]["AZURE_CONFIG_DIR"], "${{ runner.temp }}/live-evaluation-azure")
        token = next(step for step in self.evaluate["steps"] if "scripts.evaluations.live run" in step.get("run", ""))
        self.assertEqual(token["env"]["AZURE_CONFIG_DIR"], login["env"]["AZURE_CONFIG_DIR"])

    def test_only_content_free_bounded_output_survives_failure(self):
        steps = self.evaluate["steps"]
        run = next(step for step in steps if "scripts.evaluations.live run" in step.get("run", ""))
        self.assertEqual(run["if"], "${{ !cancelled() }}")
        self.assertIn("timeout 30s az account get-access-token", run["run"])
        self.assertIn('2>/dev/null', run["run"])
        self.assertIn('AI4IA_LIVE_EVAL_TOKEN="$token" python -m scripts.evaluations.live run', run["run"])
        self.assertNotIn("$GITHUB_ENV", run["run"])
        self.assertNotIn("continue-on-error", run)
        self.assertIn('echo "::add-mask::$token"', run["run"])
        self.assertNotRegex(run["run"], r"az\s+(?:role|ad|containerapp|deployment|group|monitor)\b")
        upload = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(upload["if"], "${{ always() }}")
        self.assertEqual(upload["with"]["path"], "${{ runner.temp }}/live-evaluation.json")
        self.assertEqual(upload["with"]["retention-days"], 7)
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertEqual(len([s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@")]), 1)

    def run_activation(self, **overrides):
        bash = find_bash()
        if bash is None:
            self.fail("bash is required for the executable workflow activation guard")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            script = root / "activation.sh"
            script.write_text(self.activation["steps"][0]["run"], encoding="utf-8", newline="\n")
            output, summary = root / "output", root / "summary"
            env = {
                **os.environ, "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_OUTPUT": output.as_posix(), "GITHUB_STEP_SUMMARY": summary.as_posix(),
                "LIVE_ENABLED": "", "SCHEDULE_ENABLED": "",
                "LIVE_CLIENT_ID": "synthetic-actor", "DEPLOY_CLIENT_ID": "deployment-actor",
                **overrides,
            }
            completed = subprocess.run(
                [bash, str(script)], env=env, cwd=ROOT, capture_output=True, timeout=10, check=False,
            )
            return (
                completed.returncode, output.read_text() if output.exists() else "",
                summary.read_text() if summary.exists() else "", completed.stdout,
            )

    def test_disabled_manual_and_separately_disabled_schedule_do_not_enable_worker(self):
        code, output, summary, _ = self.run_activation()
        self.assertEqual(code, 0)
        self.assertEqual(output, "enabled=false\n")
        self.assertIn("unmeasured", summary)
        code, output, summary, _ = self.run_activation(LIVE_ENABLED="true", GITHUB_EVENT_NAME="schedule")
        self.assertEqual(code, 0)
        self.assertEqual(output, "enabled=false\n")
        self.assertIn("separate schedule activation", summary)
        for event in ("workflow_dispatch", "schedule"):
            code, output, _, _ = self.run_activation(
                LIVE_ENABLED="true", GITHUB_EVENT_NAME=event, SCHEDULE_ENABLED="true",
            )
            self.assertEqual(code, 0)
            self.assertEqual(output, "enabled=true\n")

    def test_wrong_ref_or_unidentified_reused_actor_fails_closed(self):
        for overrides in (
            {"GITHUB_REF": "refs/heads/feature"},
            {"LIVE_CLIENT_ID": ""},
            {"DEPLOY_CLIENT_ID": ""},
            {"LIVE_CLIENT_ID": "DEPLOYMENT-ACTOR"},
        ):
            code, output, _, log = self.run_activation(LIVE_ENABLED="true", **overrides)
            self.assertNotEqual(code, 0)
            self.assertNotIn("enabled=true", output)
            self.assertIn(b"::error::", log)

    def test_existing_api_job_runs_only_offline_or_mocked_evaluations(self):
        document = yaml.safe_load((ROOT / ".github" / "workflows" / "app-ci.yml").read_text(encoding="utf-8"))
        scripts = "\n".join(step.get("run", "") for step in document["jobs"]["api"]["steps"])
        self.assertIn("scripts/tests/test_live_evaluations.py", scripts)
        self.assertIn("python -m scripts.evaluations run", scripts)
        self.assertNotIn("scripts.evaluations.live run", scripts)
        self.assertNotRegex(scripts, re.compile(r"get-access-token|AI4IA_LIVE_EVAL_TOKEN"))


if __name__ == "__main__":
    unittest.main()
