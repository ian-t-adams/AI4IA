import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = Path(__file__).resolve().parent
ROOT = Path.cwd()
SOURCE = HERE if (HERE / "hosted_oidc_requirements.py").exists() else HERE.parent
WORKFLOW = (HERE / "hosted_oidc_one_read.workflow.yml"
            if (HERE / "hosted_oidc_one_read.workflow.yml").exists()
            else ROOT / ".github" / "workflows" / "hosted-web-one-read.yml")
QUALITY = (HERE / "quality_snapshot.yml"
           if (HERE / "quality_snapshot.yml").exists()
           else ROOT / ".github" / "workflows" / "quality.yml")
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("hosted_oidc_requirements", SOURCE / "hosted_oidc_requirements.py")
requirements = importlib.util.module_from_spec(spec)
spec.loader.exec_module(requirements)


def package(name, dependencies=None, optional=None):
    p = {
        "name": name, "version": "1.0", "source": {"registry": "https://pypi.org/simple"},
        "wheels": [{"hash": "sha256:" + "a" * 64}],
    }
    if dependencies:
        p["dependencies"] = dependencies
    if optional:
        p["optional-dependencies"] = optional
    return p


def lock():
    return {"package": [
        package("azure-identity", [{"name": "msal"}]),
        package("msal", [{"name": "cryptography"}, {"name": "pyjwt", "extra": ["crypto"]}]),
        package("cryptography"),
        package("pyjwt", optional={"crypto": [{"name": "cryptography"}]}),
    ]}


class HostedContractTests(unittest.TestCase):
    def run_admission(self, overrides):
        workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash unavailable; hosted CI runs this control on Ubuntu")
        env = {
            "ADMIT": "false",
            "ENABLED": "",
            "REVIEWED": "",
            "CLIENT": "",
            "TENANT": "",
            "SUBSCRIPTION": "",
            "GITHUB_EVENT_NAME": "",
            "GITHUB_REF": "",
            "GITHUB_REPOSITORY": "",
            "GITHUB_SHA": "",
        }
        env.update(overrides)
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "github-output"
            env["GITHUB_OUTPUT"] = str(output_path)
            completed = subprocess.run(
                [bash, "-c", workflow["jobs"]["admission"]["steps"][0]["run"]],
                env={**os.environ, **env}, capture_output=True, text=True, timeout=5, check=False,
            )
            output = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", completed.stdout + completed.stderr)
        return completed.returncode, output

    def test_hash_checked_closure_and_refusal_for_changed_extra(self):
        data = lock()
        rendered = requirements.render(data)
        for name in ("azure-identity", "msal", "cryptography", "pyjwt"):
            self.assertIn(name + "==1.0", rendered)
        self.assertIn("--hash=sha256:" + "a" * 64, rendered)
        data["package"][1]["dependencies"][1]["extra"] = ["unexpected"]
        with self.assertRaises(ValueError):
            requirements.render(data)

    def test_workflow_manual_production_bound_and_oidc_after_gate(self):
        workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertEqual(list(workflow["on"]), ["workflow_dispatch"])
        self.assertEqual(workflow["on"]["workflow_dispatch"]["inputs"]["admit"]["default"], "false")
        self.assertEqual(workflow["permissions"], {})
        self.assertNotIn("concurrency", workflow)
        admission = workflow["jobs"]["admission"]
        read = workflow["jobs"]["read"]
        self.assertNotIn("permissions", admission)
        self.assertNotIn("concurrency", admission)
        self.assertEqual(read["needs"], "admission")
        self.assertEqual(read["if"], "needs.admission.outputs.admitted == 'true'")
        self.assertEqual(read["environment"], "production")
        self.assertEqual(read["concurrency"]["group"], "deploy-production")
        self.assertEqual(read["concurrency"]["cancel-in-progress"], "false")
        self.assertEqual(read["permissions"], {"contents": "read", "id-token": "write"})
        gate = admission["steps"][0]["run"]
        for text in ("refs/heads/main", "workflow_dispatch", "$REVIEWED", "$GITHUB_SHA",
                     "ac8d62b9-904f-4c84-9444-c41512acf74f",
                     "6907d2a4-685a-4aea-92ab-d930217467f1",
                     "e852113b-6cb5-441c-ac68-26cff884e479"):
            self.assertIn(text, gate)
        self.assertEqual(read["steps"][0]["with"]["persist-credentials"], "false")
        command = read["steps"][-1]["run"]
        self.assertIn("--require-hashes --only-binary=:all:", command)
        self.assertIn('-I scripts/hosted_oidc_read.py', command)
        self.assertNotIn("azure/login", str(workflow))

    def test_quality_runs_exact_lock_sdk_and_offline_reader_tests(self):
        quality = yaml.load(QUALITY.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        steps = quality["jobs"]["script-tests"]["steps"]
        step = next(s for s in steps if s.get("name") == "Run hosted OIDC reader offline controls")
        script = step["run"]
        self.assertIn("scripts.hosted_oidc_requirements", script)
        self.assertIn("--require-hashes --only-binary=:all:", script)
        self.assertIn('azure.identity.__version__=="1.27.0"', script)
        self.assertIn("scripts/tests/test_hosted_oidc_read.py", script)
        self.assertIn("scripts/tests/test_hosted_oidc_contract.py", script)
        self.assertNotIn("id-token", step.get("permissions", {}))
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", script)

    def test_quality_test_manifest_is_explicit_and_no_op_is_unprivileged(self):
        quality = yaml.load(QUALITY.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        read_step = next(s for s in quality["jobs"]["script-tests"]["steps"]
                         if s.get("name") == "Run hosted OIDC reader offline controls")
        self.assertIn("scripts/tests/test_hosted_oidc_read.py", read_step["run"])
        self.assertIn("scripts/tests/test_hosted_oidc_contract.py", read_step["run"])
        workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        admission = workflow["jobs"]["admission"]
        self.assertNotIn("id-token", admission.get("permissions", {}))
        self.assertNotIn("concurrency", admission)
        self.assertNotIn("concurrency", workflow)

    def test_executed_admission_noop_and_mutations(self):
        code, output = self.run_admission({"ADMIT": "false"})
        self.assertEqual((code, output), (0, "admitted=false\n"))
        base = {
            "ADMIT": "true",
            "ENABLED": "true",
            "REVIEWED": "a" * 40,
            "CLIENT": "ac8d62b9-904f-4c84-9444-c41512acf74f",
            "TENANT": "6907d2a4-685a-4aea-92ab-d930217467f1",
            "SUBSCRIPTION": "e852113b-6cb5-441c-ac68-26cff884e479",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": "ian-t-adams/AI4IA",
            "GITHUB_SHA": "a" * 40,
        }
        code, output = self.run_admission(base)
        self.assertEqual((code, output), (0, "admitted=true\n"))
        for key, value in (("ENABLED", "false"), ("GITHUB_REF", "refs/heads/other"),
                           ("GITHUB_SHA", "b" * 40), ("CLIENT", "spoofed"),
                           ("TENANT", "spoofed"), ("SUBSCRIPTION", "spoofed"),
                           ("GITHUB_EVENT_NAME", "push")):
            with self.subTest(key=key):
                code, output = self.run_admission({**base, key: value})
                self.assertEqual(code, 1)
                self.assertEqual(output, "")


if __name__ == "__main__":
    unittest.main()
