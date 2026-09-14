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
import tempfile
import unittest
from pathlib import Path

from scripts.tests._production_fixture import production_document

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "infra" / "main.bicep"
MODELS = ROOT / "infra" / "models.json"


def _compile(main: Path = MAIN) -> subprocess.CompletedProcess[str]:
    bicep = shutil.which("bicep")
    if bicep:
        command = [bicep, "build", str(main), "--stdout"]
    else:
        az = shutil.which("az")
        if not az:
            raise AssertionError(
                "Bicep clean-diagnostics tests require standalone `bicep` or Azure CLI `az`."
            )
        command = [az, "bicep", "build", "--file", str(main), "--stdout"]
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

    def test_capacity_profile_defaults_and_real_module_selection(self) -> None:
        parameter = self.template["parameters"]["modelCapacityProfile"]
        self.assertEqual(parameter["defaultValue"], "baseline")
        self.assertEqual(parameter["allowedValues"], ["baseline", "production", "maximum"])
        inputs = [
            row["input"] for row in self.template["variables"]["copy"]
            if row["name"] == "modelDeploymentsByRegion"
        ]
        self.assertEqual(len(inputs), 1)
        self.assertIn("__bicep.selectedCapacity", inputs[0])
        self.assertIn("parameters('modelCapacityProfile')", inputs[0])
        model_module = self.template["resources"]["modelDeployments"]["properties"]
        self.assertEqual(
            model_module["parameters"]["deployments"]["value"],
            "[variables('modelDeploymentsByRegion')[copyIndex()]]",
        )
        resources = model_module["template"]["resources"]
        deployment = next(r for r in resources if r["type"] == "Microsoft.CognitiveServices/accounts/deployments")
        self.assertIn("capacity", deployment["sku"]["capacity"])

    def test_claude_entitlement_defaults_off(self) -> None:
        self.assertFalse(self.template["parameters"]["claudeEnabled"]["defaultValue"])
        self.assertIn("deployableCatalog", self.template["variables"])
        self.assertIn(
            "claudeEnabled",
            json.dumps(self.template["variables"]["deployableCatalog"]),
        )

    def test_tool_auto_approval_is_an_explicit_default_off_api_gate(self) -> None:
        self.assertFalse(
            self.template["parameters"]["toolAutoApproveEnabled"]["defaultValue"]
        )
        module = self.template["resources"]["api"]["properties"]
        self.assertEqual(
            module["parameters"]["toolAutoApproveEnabled"]["value"],
            "[parameters('toolAutoApproveEnabled')]",
        )
        api = module["template"]
        self.assertFalse(api["parameters"]["toolAutoApproveEnabled"]["defaultValue"])
        self.assertEqual(
            api["variables"]["toolApprovalEnv"],
            [
                {
                    "name": "AI4IA_TOOL_AUTO_APPROVE_ENABLED",
                    "value": "[string(parameters('toolAutoApproveEnabled'))]",
                }
            ],
        )
        self.assertIn(
            "variables('toolApprovalEnv')",
            json.dumps(api["variables"]["apiEnv"]),
        )

    def test_resumable_deletion_defaults_off_and_reaches_api(self) -> None:
        module = self.template["resources"]["api"]["properties"]
        api = module["template"]
        for name, default in (
            ("sessionDeletionEnabled", False), ("sessionDeletionRolloutId", "")
        ):
            self.assertEqual(self.template["parameters"][name]["defaultValue"], default)
            self.assertEqual(api["parameters"][name]["defaultValue"], default)
            self.assertEqual(module["parameters"][name]["value"], f"[parameters('{name}')]")
        self.assertEqual(api["variables"]["sessionDeletionEnv"], [
            {
                "name": "AI4IA_SESSION_DELETION_ENABLED",
                "value": "[string(parameters('sessionDeletionEnabled'))]",
            },
            {
                "name": "AI4IA_SESSION_DELETION_ROLLOUT_ID",
                "value": "[parameters('sessionDeletionRolloutId')]",
            },
        ])
        self.assertIn("variables('sessionDeletionEnv')", json.dumps(api["variables"]["apiEnv"]))

    def test_webiq_limits_and_endpoint_reach_the_api_without_exposing_credentials(self) -> None:
        module = self.template["resources"]["api"]["properties"]
        api = module["template"]
        for name, default, maximum in (
            ("webSearchMaxResults", 5, 50),
            ("webSearchMaxContentChars", 6000, 500000),
        ):
            with self.subTest(parameter=name):
                self.assertEqual(self.template["parameters"][name]["defaultValue"], default)
                self.assertEqual(self.template["parameters"][name]["minValue"], 1)
                self.assertEqual(self.template["parameters"][name]["maxValue"], maximum)
                self.assertEqual(
                    module["parameters"][name]["value"], f"[parameters('{name}')]"
                )
                self.assertEqual(api["parameters"][name]["defaultValue"], default)
                self.assertEqual(api["parameters"][name]["maxValue"], maximum)
                self.assertIn(
                    f"string(parameters('{name}'))",
                    json.dumps(api["variables"]["webSearchEnv"]),
                )
        self.assertEqual(
            module["parameters"]["webIqBaseUrl"]["value"], "[parameters('webIqBaseUrl')]"
        )
        self.assertEqual(api["parameters"]["webIqApiKey"]["type"].lower(), "securestring")
        emitted = json.dumps(api["variables"]["webSearchEnv"])
        self.assertIn("AI4IA_WEB_SEARCH_MAX_RESULTS", emitted)
        self.assertIn("AI4IA_WEB_SEARCH_MAX_CONTENT_CHARS", emitted)
        self.assertIn("AI4IA_WEBIQ_BASE_URL", emitted)
        self.assertNotIn("value', parameters('webIqApiKey')", emitted)

    def test_media_gates_emit_both_boolean_values_independently_of_storage(self) -> None:
        module = self.template["resources"]["api"]["properties"]
        api = module["template"]
        expected = []
        for parameter, env_name in (
            ("imageGenerationEnabled", "AI4IA_IMAGE_GENERATION_ENABLED"),
            ("videoGenerationEnabled", "AI4IA_VIDEO_GENERATION_ENABLED"),
        ):
            self.assertFalse(self.template["parameters"][parameter]["defaultValue"])
            self.assertEqual(
                module["parameters"][parameter]["value"], f"[parameters('{parameter}')]"
            )
            self.assertFalse(api["parameters"][parameter]["defaultValue"])
            expected.append(
                {"name": env_name, "value": f"[string(parameters('{parameter}'))]"}
            )
        self.assertEqual(api["variables"].get("mediaFeatureEnv"), expected)
        self.assertIn("variables('mediaFeatureEnv')", api["variables"]["apiEnv"])

    def test_openapi_false_is_emitted_rather_than_treated_as_unset(self) -> None:
        module = self.template["resources"]["api"]["properties"]
        api = module["template"]
        self.assertEqual(
            module["parameters"]["apiOpenapiEnabled"]["value"],
            "[parameters('apiOpenapiEnabled')]",
        )
        self.assertEqual(
            api["variables"]["openapiEnv"],
            [
                {
                    "name": "AI4IA_OPENAPI_ENABLED",
                    "value": "[string(parameters('apiOpenapiEnabled'))]",
                }
            ],
        )
        self.assertIn("variables('openapiEnv')", api["variables"]["apiEnv"])

    def test_search_metrics_use_the_search_deployment_location(self) -> None:
        self.assertEqual(
            self.template["variables"].get("effectiveSearchLocation"),
            "[if(empty(parameters('searchLocation')), parameters('location'), "
            "parameters('searchLocation'))]",
        )
        search = self.template["resources"]["search"]["properties"]
        api = self.template["resources"]["api"]["properties"]
        self.assertEqual(
            search["parameters"]["location"]["value"],
            "[variables('effectiveSearchLocation')]",
        )
        self.assertEqual(
            api["parameters"]["metricsSearchLocation"]["value"],
            "[variables('effectiveSearchLocation')]",
        )
        emitted = api["template"]["variables"]["resourceMetricsEnv"]
        self.assertIn("AI4IA_METRICS_SEARCH_ENDPOINT", emitted)
        self.assertIn(
            "format('https://{0}.metrics.monitor.azure.com', "
            "if(empty(parameters('metricsSearchLocation')), parameters('location'), "
            "parameters('metricsSearchLocation')))",
            emitted,
        )

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
        api_parameters = self.template["resources"]["api"]["properties"]["parameters"]
        self.assertEqual(
            api_parameters["claudeEnabled"]["value"],
            "[parameters('claudeEnabled')]",
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


class ProductionCapacityCompiledTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        shutil.copy2(ROOT / "infra" / "capacity.bicep", self.directory / "capacity.bicep")
        self.document = production_document()

    def build_parameters(
        self, document: dict, profiles: tuple[str, ...] = ("baseline", "maximum", "production"),
    ) -> subprocess.CompletedProcess[str]:
        (self.directory / "models.json").write_text(json.dumps(document), encoding="utf-8")
        source = """using none
import { selectedCapacity } from './capacity.bicep'
var models = loadJsonContent('models.json')
param selections = {
"""
        source += "\n".join(
            f"  {profile}: map(models.catalog[0].deployments, d => selectedCapacity(d, '{profile}'))"
            for profile in profiles
        ) + "\n}\n"
        parameters = self.directory / "fixture.bicepparam"
        parameters.write_text(source, encoding="utf-8")
        bicep, az = shutil.which("bicep"), shutil.which("az")
        if bicep:
            command = [bicep, "build-params", str(parameters), "--stdout"]
        elif az:
            command = [az, "bicep", "build-params", "--file", str(parameters), "--stdout"]
        else:
            self.fail("The same Bicep compiler used by infra-validate is required.")
        return subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)

    def test_real_bicep_evaluates_configured_profile_and_maximum_fallback(self) -> None:
        from scripts.tests.test_model_capacity_profile import preflight

        del self.document["catalog"][0]["deployments"][1]["maxCapacity"]
        result = self.build_parameters(self.document)
        self.assertEqual(result.returncode, 0, result.stderr)
        envelope = json.loads(result.stdout)
        parameters = json.loads(envelope["parametersJson"]) if "parametersJson" in envelope else envelope
        actual = parameters["parameters"]["selections"]["value"]
        self.assertEqual(actual, {"baseline": [10, 10], "maximum": [100, 10], "production": [40, 20]})
        for profile, capacities in actual.items():
            required = preflight.catalog_requirements(self.document, capacity_profile=profile)
            self.assertEqual(capacities, [d["capacity"] for region in required.values() for d in region])

    def test_missing_production_metadata_is_a_real_bicep_error_not_zero_or_fallback(self) -> None:
        allowed = self.build_parameters(self.document)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        for missing in ("production", "capacity"):
            document = production_document()
            deployment = document["catalog"][0]["deployments"][0]
            if missing == "production":
                del deployment["production"]
            else:
                del deployment["production"]["capacity"]
            with self.subTest(missing=missing):
                unchanged = self.build_parameters(document, ("baseline", "maximum"))
                self.assertEqual(unchanged.returncode, 0, unchanged.stderr)
                denied = self.build_parameters(document)
                self.assertNotEqual(denied.returncode, 0)
                self.assertIn("Error", denied.stderr)

    def test_full_root_compiles_with_the_same_configured_catalog(self) -> None:
        infra = self.directory / "infra"
        shutil.copytree(ROOT / "infra", infra)
        document = production_document(include_cu=True)
        (infra / "models.json").write_text(json.dumps(document), encoding="utf-8")
        result = _compile(infra / "main.bicep")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotRegex(result.stderr, r"\(\d+,\d+\)\s*:\s*(?:Warning|Error)")
        template = json.loads(result.stdout)
        self.assertIn(document, template["variables"].values())
        self.assertIn("production", template["parameters"]["modelCapacityProfile"]["allowedValues"])
        self.assertIn("__bicep.selectedCapacity", json.dumps(template["variables"]["copy"]))


if __name__ == "__main__":
    unittest.main()
