"""Runtime safety contracts for Azure-facing PowerShell operator scripts."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
OTHER_SUBSCRIPTION = "22222222-2222-2222-2222-222222222222"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
WRONG_SUBSCRIPTION_RE = re.compile(
    r"does\W+not\W+exactly\W+match\W+requested\W+subscription"
)

CASES = {
    "teardown.ps1": {
        "Subscription": SUBSCRIPTION,
        "ResourceGroups": ["rg-ai4ia-test"],
        "CognitiveAccountNames": ["foundry-ai4ia-test"],
        "KeyVaultNames": ["kv-ai4ia-test"],
    },
    "purge-soft-deleted.ps1": {
        "Subscription": SUBSCRIPTION,
        "CognitiveAccountNames": ["foundry-ai4ia-test"],
        "KeyVaultNames": ["kv-ai4ia-test"],
    },
    "capture-data-recovery-state.ps1": {
        "Subscription": SUBSCRIPTION,
        "ResourceGroup": "rg-ai4ia-test",
        "SkipBlobSizes": True,
    },
    "inventory.ps1": {
        "Subscription": SUBSCRIPTION,
        "ResourceGroup": "rg-ai4ia-test",
    },
    "seed-models.ps1": {
        "Subscription": SUBSCRIPTION,
        "Regions": ["eastus"],
    },
}

RUNNER = r"""
function global:az {
    $call = [object[]] $args
    Add-Content -LiteralPath $env:AZ_STUB_LOG -Value (
        ConvertTo-Json -InputObject $call -Compress
    )
    $key = $args -join ' '

    if ($env:AZ_STUB_FAIL_PREFIX -and $key.StartsWith($env:AZ_STUB_FAIL_PREFIX)) {
        [Console]::Error.WriteLine('injected Azure CLI failure')
        $global:LASTEXITCODE = 23
        return
    }

    $global:LASTEXITCODE = 0
    if ($key.StartsWith('account show ')) {
        Write-Output $env:AZ_STUB_ACTIVE_SUBSCRIPTION
        return
    }
    if ($key.StartsWith('group exists ')) {
        Write-Output 'false'
        return
    }
    if ($env:AZ_STUB_PURGE_FIXTURE -eq '1' -and
        $key.StartsWith('cognitiveservices account list-deleted ')) {
        Write-Output '[{"name":"foundry-ai4ia-test","location":"eastus","id":"/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-ai4ia-test/providers/Microsoft.CognitiveServices/accounts/foundry-ai4ia-test"},{"name":"other-stack","location":"eastus","id":"/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-other/providers/Microsoft.CognitiveServices/accounts/other-stack"}]'
        return
    }
    if ($env:AZ_STUB_PURGE_FIXTURE -eq '1' -and $key.StartsWith('keyvault list-deleted ')) {
        $fixtureName = if ($env:AZ_STUB_SAME_NAME_FIXTURE -eq '1') {
            'foundry-ai4ia-test'
        } else {
            'kv-ai4ia-test'
        }
        Write-Output ('[{"name":"' + $fixtureName + '","properties":{"location":"eastus"}},{"name":"other-vault","properties":{"location":"eastus"}}]')
        return
    }
    if ($key.StartsWith('cognitiveservices account list ') -and $args -contains 'tsv') {
        return
    }
    if ($key -match ' list( |$)' -or $key -match ' list-deleted( |$)') {
        Write-Output '[]'
        return
    }
}

$parameterObject = $env:SCRIPT_PARAMETERS | ConvertFrom-Json
$scriptParameters = @{}
foreach ($property in $parameterObject.PSObject.Properties) {
    $scriptParameters[$property.Name] = $property.Value
}
& $env:TARGET_SCRIPT @scriptParameters
if (-not $?) {
    exit 1
}
"""


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell is required")
class AzureCliSafetyExecutionTests(unittest.TestCase):
    def run_script(
        self,
        script_name: str,
        parameters: dict[str, object],
        *,
        active_subscription: str = SUBSCRIPTION,
        fail_prefix: str = "",
        purge_fixture: bool = False,
        same_name_fixture: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], list[list[str]], set[str]]:
        with tempfile.TemporaryDirectory(prefix=".azure-cli-safety-", dir=ROOT) as directory:
            work = Path(directory)
            runner = work / "runner.ps1"
            log = work / "az-calls.jsonl"
            runner.write_text(RUNNER, encoding="utf-8")

            actual_parameters = dict(parameters)
            if "OutDir" not in actual_parameters and script_name in {
                "capture-data-recovery-state.ps1",
                "inventory.ps1",
                "seed-models.ps1",
            }:
                actual_parameters["OutDir"] = str(work / "output")

            env = os.environ.copy()
            env.update(
                {
                    "AZ_STUB_ACTIVE_SUBSCRIPTION": active_subscription,
                    "AZ_STUB_FAIL_PREFIX": fail_prefix,
                    "AZ_STUB_LOG": str(log),
                    "AZ_STUB_PURGE_FIXTURE": "1" if purge_fixture else "0",
                    "AZ_STUB_SAME_NAME_FIXTURE": "1" if same_name_fixture else "0",
                    "SCRIPT_PARAMETERS": json.dumps(actual_parameters),
                    "TARGET_SCRIPT": str(SCRIPTS / script_name),
                }
            )
            result = subprocess.run(
                ["pwsh", "-NoProfile", "-File", str(runner)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            calls = (
                [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
                if log.exists()
                else []
            )
            captured_files = {
                path.name for path in (work / "output").rglob("*.json")
            } if (work / "output").exists() else set()
            return result, calls, captured_files

    def test_every_script_refuses_wrong_subscription_before_other_azure_calls(self) -> None:
        for script_name, parameters in CASES.items():
            with self.subTest(script=script_name):
                result, calls, _ = self.run_script(
                    script_name,
                    parameters,
                    active_subscription=OTHER_SUBSCRIPTION,
                )
                self.assertNotEqual(result.returncode, 0)
                clean_error = ANSI_RE.sub("", result.stderr)
                self.assertRegex(clean_error, WRONG_SUBSCRIPTION_RE)
                self.assertEqual(
                    [call[:2] for call in calls],
                    [["account", "set"], ["account", "show"]],
                )

    def test_every_script_continues_after_matching_subscription_control(self) -> None:
        for script_name, parameters in CASES.items():
            with self.subTest(script=script_name):
                result, calls, _ = self.run_script(script_name, parameters)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    [call[:2] for call in calls[:2]],
                    [["account", "set"], ["account", "show"]],
                )
                self.assertGreater(len(calls), 2, "control never reached the script's Azure reads")
                for call in calls[2:]:
                    if call[0] == "account":
                        continue
                    self.assertIn("--subscription", call)
                    index = call.index("--subscription")
                    self.assertEqual(call[index + 1], SUBSCRIPTION)

    def test_nonzero_azure_cli_exit_is_never_treated_as_success(self) -> None:
        result, calls, _ = self.run_script(
            "seed-models.ps1",
            CASES["seed-models.ps1"],
            fail_prefix="cognitiveservices model list",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Azure CLI failed with exit code 23", result.stderr)
        self.assertEqual(calls[-1][:3], ["cognitiveservices", "model", "list"])

    def test_purge_rejects_wildcard_and_empty_names_before_azure(self) -> None:
        for parameter in ("CognitiveAccountNames", "KeyVaultNames"):
            for unsafe_names in (["*"], ["?"], ["ai4ia*"], ["   "], []):
                with self.subTest(parameter=parameter, names=unsafe_names):
                    parameters = dict(CASES["purge-soft-deleted.ps1"])
                    parameters[parameter] = unsafe_names
                    result, calls, _ = self.run_script(
                        "purge-soft-deleted.ps1",
                        parameters,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(calls, [])

    def test_purge_executes_only_exact_approved_names(self) -> None:
        parameters = dict(CASES["purge-soft-deleted.ps1"])
        parameters["Force"] = True
        result, calls, _ = self.run_script(
            "purge-soft-deleted.ps1",
            parameters,
            purge_fixture=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        purge_calls = [call for call in calls if "purge" in call]
        self.assertEqual(len(purge_calls), 2)
        purged_names = [call[call.index("--name") + 1] for call in purge_calls]
        self.assertEqual(purged_names, ["foundry-ai4ia-test", "kv-ai4ia-test"])

    def test_same_name_approval_is_scoped_to_resource_type(self) -> None:
        parameters = {
            "Subscription": SUBSCRIPTION,
            "CognitiveAccountNames": ["foundry-ai4ia-test"],
            "KeyVaultNames": ["not-the-fixture-vault"],
            "Force": True,
        }
        result, calls, _ = self.run_script(
            "purge-soft-deleted.ps1",
            parameters,
            purge_fixture=True,
            same_name_fixture=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        purge_calls = [call for call in calls if "purge" in call]
        self.assertEqual(len(purge_calls), 1)
        self.assertEqual(
            purge_calls[0][purge_calls[0].index("--name") + 1],
            "foundry-ai4ia-test",
        )

    def test_inventory_all_success_reports_complete(self) -> None:
        result, _, captured_files = self.run_script(
            "inventory.ps1",
            CASES["inventory.ps1"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Inventory complete:", result.stdout)
        self.assertNotIn("Inventory INCOMPLETE:", result.stdout)
        self.assertIn("resources.json", captured_files)

    def test_inventory_failed_section_reports_incomplete_and_keeps_successes(self) -> None:
        result, _, captured_files = self.run_script(
            "inventory.ps1",
            CASES["inventory.ps1"],
            fail_prefix="resource list",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Inventory INCOMPLETE:", result.stdout)
        self.assertNotIn("Inventory complete:", result.stdout)
        self.assertIn(
            "cognitive-accounts.json",
            captured_files,
            "a later successful section must remain captured",
        )

    def test_inventory_enumeration_failure_keeps_independent_captures(self) -> None:
        result, calls, captured_files = self.run_script(
            "inventory.ps1",
            CASES["inventory.ps1"],
            fail_prefix=(
                "cognitiveservices account list --resource-group rg-ai4ia-test "
                "--query"
            ),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Inventory INCOMPLETE:", result.stdout)
        self.assertIn("cognitive-account-names", result.stdout + result.stderr)
        self.assertIn("keyvaults.json", captured_files)
        self.assertIn("soft-deleted-cognitive.json", captured_files)
        self.assertIn("soft-deleted-keyvaults.json", captured_files)
        self.assertFalse(
            any(name.startswith("deployments-") for name in captured_files)
        )
        failed_index = next(
            index
            for index, call in enumerate(calls)
            if "--query" in call and call[:3] == ["cognitiveservices", "account", "list"]
        )
        self.assertTrue(
            any(call[:2] == ["keyvault", "list"] for call in calls[failed_index + 1 :])
        )


class AzureCliSafetySourceTests(unittest.TestCase):
    def test_wrong_subscription_error_match_survives_linux_powershell_wrapping(self) -> None:
        rendered = (
            "\x1b[31;1mActive subscription does\x1b[0m\n"
            "\x1b[31;1m| not exactly match requested subscription\x1b[0m"
        )
        self.assertRegex(ANSI_RE.sub("", rendered), WRONG_SUBSCRIPTION_RE)

    def test_scripts_share_the_checked_azure_cli_helper(self) -> None:
        for script_name in CASES:
            with self.subTest(script=script_name):
                text = (SCRIPTS / script_name).read_text(encoding="utf-8")
                self.assertIn("azure-cli.ps1", text)
                self.assertIn("Assert-AzureSubscription -Subscription $Subscription", text)
                self.assertIsNone(
                    re.search(r"^\s*(?:&\s+)?az\s", text, re.MULTILINE),
                    "Azure CLI calls must use Invoke-AzureCli so exit codes are checked",
                )
        helper = (SCRIPTS / "azure-cli.ps1").read_text(encoding="utf-8")
        self.assertIn(
            "$effectiveArguments += @('--subscription', $script:AzureCliSubscription)",
            helper,
        )

    def test_purge_selection_is_exact_not_pattern_based(self) -> None:
        text = (SCRIPTS / "purge-soft-deleted.ps1").read_text(encoding="utf-8")
        self.assertIn("$CognitiveAccountNames -notcontains $c.name", text)
        self.assertIn("$KeyVaultNames -notcontains $v.name", text)
        self.assertNotIn("-like", text)


if __name__ == "__main__":
    unittest.main()
