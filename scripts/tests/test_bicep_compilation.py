"""Compile Bicep and pin warning-free, behavior-preserving ARM output.

The tests invoke the same pinned compiler installed by infra-validate. They inspect
the generated ARM template so source-only edits cannot claim safety while changing
the conditional outputs or endpoint normalization that Azure evaluates.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "infra" / "main.bicep"
MODELS = ROOT / "infra" / "models.json"


def _compile() -> subprocess.CompletedProcess[str]:
    bicep = shutil.which("bicep")
    if bicep:
        command = [bicep, "build", str(MAIN), "--stdout"]
    else:
        az = shutil.which("az")
        if not az:
            raise AssertionError(
                "Bicep clean-diagnostics tests require standalone `bicep` or Azure CLI `az`."
            )
        command = [az, "bicep", "build", "--file", str(MAIN), "--stdout"]
    return subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )


class BicepCompiledBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build = _compile()
        if cls.build.returncode != 0:
            raise AssertionError(
                f"Bicep compilation failed ({cls.build.returncode}):\n{cls.build.stderr}"
            )
        cls.template = json.loads(cls.build.stdout)

    def test_build_has_no_bicep_diagnostics(self) -> None:
        # Azure CLI can print a version-update notice. A compiler diagnostic is a
        # source-positioned Warning/Error and must fail even though `bicep build`
        # normally exits zero for warnings.
        diagnostics = re.findall(
            r"(?im)^.*\(\d+,\d+\)\s*:\s*(?:warning|error)\s+\S+.*$",
            self.build.stderr,
        )
        self.assertEqual(
            diagnostics,
            [],
            "Bicep emitted diagnostics despite a successful exit code:\n"
            + "\n".join(diagnostics),
        )

    def test_location_allowlist_matches_catalog_primary_regions(self) -> None:
        models = json.loads(MODELS.read_text(encoding="utf-8"))
        expected = sorted(
            name
            for name, config in models["regions"].items()
            if config.get("primary") is True
        )
        actual = sorted(self.template["parameters"]["location"]["allowedValues"])
        self.assertEqual(actual, expected)
        self.assertIn("swedencentral", actual)

    def test_null_forgiving_access_preserves_conditional_durable_outputs(self) -> None:
        parameters = self.template["resources"]["api"]["properties"]["parameters"]
        self.assertEqual(
            parameters["durableTaskEndpoint"],
            "[if(parameters('enableDurableWorkflows'), createObject('value', "
            "reference('durabletask').outputs.endpoint.value), createObject('value', ''))]",
        )
        self.assertEqual(
            parameters["durableTaskHubName"],
            "[if(parameters('enableDurableWorkflows'), createObject('value', "
            "reference('durabletask').outputs.taskHubName.value), createObject('value', ''))]",
        )

    def test_endpoint_normalization_has_contracts_and_nonnegative_bounds(self) -> None:
        gateway = self.template["resources"]["gateway"]["properties"]["template"]
        for parameter in (
            "primaryFoundryEndpoint",
            "speechVoiceLiveAccountEndpoint",
        ):
            self.assertEqual(gateway["parameters"][parameter]["minLength"], 1)

        realtime = gateway["variables"]["primaryFoundryRealtimeWssUrl"]
        speech = gateway["variables"]["speechVoiceLiveAccountBase"]
        self.assertIn(
            "max(sub(length(parameters('primaryFoundryEndpoint')), 1), 0)",
            realtime,
        )
        self.assertIn(
            "max(sub(length(parameters('speechVoiceLiveAccountEndpoint')), 1), 0)",
            speech,
        )
        self.assertIn("'https://', 'wss://'", realtime)

    def test_primary_cu_outputs_use_the_same_records_sent_to_model_modules(self) -> None:
        module_parameters = self.template["resources"]["modelDeployments"]["properties"][
            "parameters"
        ]
        module_value = module_parameters["deployments"]["value"]
        self.assertEqual(
            module_value,
            "[variables('modelDeploymentsByRegion')[copyIndex()]]",
        )
        self.assertEqual(
            module_parameters["claudeOrganizationName"]["value"],
            "[parameters('claudeOrganizationName')]",
        )
        self.assertEqual(
            module_parameters["claudeCountryCode"]["value"],
            "[parameters('claudeCountryCode')]",
        )
        self.assertEqual(
            module_parameters["claudeIndustry"]["value"],
            "[parameters('claudeIndustry')]",
        )
        variables = self.template["variables"]
        self.assertEqual(
            variables["primaryModelDeployments"],
            "[variables('modelDeploymentsByRegion')[variables('primaryFoundryIndex')]]",
        )
        self.assertIn("'gpt-5.2'", variables["primaryCuCompletionDeployment"])
        self.assertIn(
            "'text-embedding-3-large'",
            variables["primaryCuEmbeddingDeployment"],
        )
        outputs = self.template["outputs"]
        expected_models = outputs["AZURE_EXPECTED_MODEL_DEPLOYMENTS"]["copy"]["input"]
        self.assertIn(
            "variables('modelDeploymentsByRegion')",
            expected_models["deploymentNames"],
        )
        self.assertIn("deploymentName", expected_models["deploymentNames"])
        self.assertEqual(
            outputs["AZURE_PRIMARY_FOUNDRY_REGION"]["value"],
            "[parameters('location')]",
        )
        self.assertEqual(
            outputs["AZURE_CONTENT_UNDERSTANDING_COMPLETION_DEPLOYMENT"]["value"],
            "[variables('primaryCuCompletionDeployment').deploymentName]",
        )
        self.assertEqual(
            outputs["AZURE_CONTENT_UNDERSTANDING_EMBEDDING_DEPLOYMENT"]["value"],
            "[variables('primaryCuEmbeddingDeployment').deploymentName]",
        )


if __name__ == "__main__":
    unittest.main()
