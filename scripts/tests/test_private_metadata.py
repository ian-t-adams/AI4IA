"""Compiled-source metadata, native masks and post-create proof controls."""
from copy import deepcopy
import json
import unittest
import jmespath

from scripts import _private_metadata as metadata
from scripts.tests.test_bicep_compilation import ROOT, _compile

SUB = "11111111-1111-4111-8111-111111111111"
GROUP = "rg-ai4ia-test"
PREFIX = f"/subscriptions/{SUB}/resourceGroups/{GROUP}/providers/"
OLD = PREFIX + "Microsoft.App/containerApps/ca-api-test"
NEW = PREFIX + "Microsoft.App/containerApps/ca-api-private-test"
IDENTITY = PREFIX + "Microsoft.ManagedIdentity/userAssignedIdentities/id-api-test"


def inputs(template):
    defaults = {"string": "", "bool": False, "int": 0, "array": [], "object": {}}
    params = {name: {"value": deepcopy(row.get("defaultValue", defaults.get(row["type"], "")))}
              for name, row in template["parameters"].items() if not row["type"].casefold().startswith("secure")}
    params.update({name: {"value": value} for name, value in {
        "environmentName": "test", "recoverySource": "legacy", "recoveryCandidate": True,
        "privateCosmosRequired": True, "webSearchEnabled": True,
        "recoverySecretDescriptors": [{"name": "model-gateway-api-key"}, {"name": "webiq-api-key"}],
    }.items()})
    baseline = {"id": OLD, "properties": {
        "configuration": {"secrets": [{"name": "model-gateway-api-key"}, {"name": "webiq-api-key"}]},
        "template": {"containers": [{"name": "api", "env": []}]},
    }}
    evaluator = metadata.MetadataEvaluator(template, params, SUB, GROUP, baseline)
    rows = evaluator.value(template["resources"]["apiApp"]["properties"]["template"]["containers"][0]["env"], "env")
    # Freeze the unmodified compiled control before any tested source mutation.
    baseline["properties"]["template"]["containers"][0]["env"] = [
        {"name": row["name"], "secretRef": row["secretRef"]} if "secretRef" in row else
        {"name": row["name"], "value": "" if row["value"].empty else "synthetic-public-value"}
        for row in rows if row["name"] not in ("AI4IA_PRIVATE_RECOVERY_CANDIDATE", "AI4IA_PRIVATE_COSMOS_REQUIRED")
    ]
    return params, baseline, {"subscription": SUB, "resourceGroup": GROUP, "environment": "test"}


def native_app(descriptor):
    return {"properties": {
        "configuration": {"secrets": [{"name": "*******", "value": "*******"} for _ in descriptor["secrets"]]},
        "template": {"containers": [{"name": "api", "env": [
            *[{"name": name, "secretRef": "*******"} for name in descriptor["envSecretRefs"]],
            *[{"name": name, **({} if name in descriptor["emptyEnvValues"] else {"value": "public"})}
              for name in descriptor["envValueNames"]],
        ]}]},
    }}


def live_snapshot(descriptor, app_id=NEW):
    return {"id": app_id, "revision": "ca-api-private-test--candidate",
            "flags": [{"name": name, "value": "true"}
                      for name in ("AI4IA_PRIVATE_RECOVERY_CANDIDATE", "AI4IA_PRIVATE_COSMOS_REQUIRED")],
            "secrets": [
                {"name": row["name"], **({"keyVaultUrl": row["keyVaultUrl"], "identity": row["identity"]}
                                       if row["kind"] == "keyVault" else {})}
                for row in descriptor["secrets"]
            ],
            "containers": [{"name": "api", "env": [
                *[{"name": name, "secretRef": ref, "hasRef": True, "hasValue": False, "emptyValue": False}
                  for name, ref in descriptor["envSecretRefs"].items()],
                *[{"name": name, "secretRef": None, "hasRef": False, "hasValue": True,
                   "emptyValue": name in descriptor["emptyEnvValues"]} for name in descriptor["envValueNames"]],
            ]}]}


class PrivateMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiled = _compile(ROOT / "infra/modules/api.bicep")
        if compiled.returncode:
            raise AssertionError(compiled.stderr)
        cls.template = json.loads(compiled.stdout)

    def descriptor(self):
        params, baseline, binding = inputs(self.template)
        result = metadata.source_descriptor(self.template, params, binding, baseline)
        return params, baseline, binding, result

    def test_actual_compiled_source_derives_names_origins_and_refs(self):
        _, _, _, descriptor = self.descriptor()
        self.assertEqual(descriptor["envSecretRefs"], {
            "AI4IA_MODEL_GATEWAY_API_KEY": "model-gateway-api-key", "AI4IA_WEBIQ_API_KEY": "webiq-api-key",
        })
        self.assertNotIn("AI4IA_WEBIQ_USE_ENTRA", descriptor["envValueNames"])
        self.assertIn("AI4IA_PRIVATE_RECOVERY_CANDIDATE", descriptor["envValueNames"])
        self.assertEqual(descriptor["valueOrigins"], [
            {"name": name, "appId": OLD.casefold(), "secretName": name}
            for name in ("model-gateway-api-key", "webiq-api-key")
        ])

    def test_flags_are_lowercase_and_webiq_modes_remain_exclusive(self):
        params, baseline, _ = inputs(self.template)
        expression = self.template["resources"]["apiApp"]["properties"]["template"]["containers"][0]["env"]
        for enabled in (True, False):
            supplied = deepcopy(params)
            supplied["recoveryCandidate"]["value"] = supplied["privateCosmosRequired"]["value"] = enabled
            rows = metadata.MetadataEvaluator(self.template, supplied, SUB, GROUP, baseline).value(expression)
            values = {row["name"]: row for row in rows}
            for name in ("AI4IA_PRIVATE_RECOVERY_CANDIDATE", "AI4IA_PRIVATE_COSMOS_REQUIRED"):
                self.assertEqual(values[name]["value"], "true" if enabled else "false")
            self.assertIn("AI4IA_WEBIQ_API_KEY", values)
            self.assertNotIn("AI4IA_WEBIQ_USE_ENTRA", values)
        params["recoverySecretDescriptors"]["value"] = [{"name": "model-gateway-api-key"}]
        rows = metadata.MetadataEvaluator(self.template, params, SUB, GROUP, baseline).value(expression)
        values = {row["name"]: row for row in rows}
        self.assertNotIn("AI4IA_WEBIQ_API_KEY", values)
        self.assertEqual(values["AI4IA_WEBIQ_USE_ENTRA"]["value"], "true")

    def test_raw_query_requests_only_secret_metadata_and_env_modes(self):
        _, baseline, _, descriptor = self.descriptor()
        baseline["properties"]["latestRevisionName"] = "ca-api-test--old"
        raw = deepcopy(baseline)
        raw["properties"]["configuration"]["secrets"][0]["value"] = "secret-value-must-not-leave"
        raw["properties"]["template"]["containers"][0]["env"].append({"name": "EXTRA", "value": "private-value"})
        projected = jmespath.search(metadata.RAW_METADATA_QUERY, raw)
        self.assertNotIn("secret-value-must-not-leave", json.dumps(projected))
        self.assertNotIn("private-value", json.dumps(projected))
        with self.assertRaises(metadata.MetadataError):
            metadata.validate_live_metadata(projected, descriptor, OLD, baseline=True)
        raw["properties"]["template"]["containers"][0]["env"].pop()
        metadata.validate_live_metadata(jmespath.search(metadata.RAW_METADATA_QUERY, raw), descriptor, OLD, baseline=True)

    def test_source_ref_or_value_origin_drift_refuses(self):
        params, baseline, binding, _ = self.descriptor()
        for fault in ("reference", "source", "source-key", "secret_parameter", "cycle", "unsupported"):
            template, supplied = deepcopy(self.template), deepcopy(params)
            if fault == "reference":
                template["variables"]["gatewayKeyEnv"] = template["variables"]["gatewayKeyEnv"].replace(
                    "'model-gateway-api-key'", "'webiq-api-key'",
                )
            elif fault == "source":
                template["resources"]["secretSource"]["name"] = "foreign-app"
            elif fault == "source-key":
                configuration = template["resources"]["apiApp"]["properties"]["configuration"]
                configuration["secrets"] = configuration["secrets"].replace(
                    "equals(lambdaVariables('current').name, lambdaVariables('secret').name)",
                    "equals(lambdaVariables('current').name, 'webiq-api-key')",
                )
            elif fault == "secret_parameter":
                supplied["modelGatewayApiKey"] = {"value": "never-log-this-secret"}
            elif fault == "cycle":
                template["variables"]["apiEnv"] = "[variables('apiEnv')]"
            else:
                template["variables"]["apiEnv"] = "[unapprovedFunction()]"
            with self.subTest(fault=fault), self.assertRaises(metadata.MetadataError) as caught:
                metadata.source_descriptor(template, supplied, binding, baseline)
            self.assertNotIn("never-log-this-secret", str(caught.exception))

    def test_key_vault_source_kind_and_identity_are_preserved(self):
        params, baseline, binding = inputs(self.template)
        row = {"name": "webiq-api-key", "keyVaultUrl": "https://synthetic.vault.azure.net/secrets/webiq", "identity": IDENTITY}
        params["recoverySecretDescriptors"]["value"][1] = row
        baseline["properties"]["configuration"]["secrets"][1] = row
        descriptor = metadata.source_descriptor(self.template, params, binding, baseline)
        self.assertEqual(descriptor["secrets"][1], {**row, "identity": IDENTITY.casefold(), "kind": "keyVault"})
        self.assertEqual(len(descriptor["valueOrigins"]), 1)
        params["recoverySecretDescriptors"]["value"][1] = {**row, "identity": IDENTITY + "-other"}
        with self.assertRaises(metadata.MetadataError):
            metadata.source_descriptor(self.template, params, binding, baseline)

    def test_native_masks_are_structural_not_positional_reconstruction(self):
        _, _, _, descriptor = self.descriptor()
        app = native_app(descriptor)
        metadata.validate_native_metadata(app, descriptor)
        app["properties"]["configuration"]["secrets"].reverse()
        app["properties"]["template"]["containers"][0]["env"].reverse()
        metadata.validate_native_metadata(app, descriptor)
        for fault in ("count", "name", "ref", "value_swap", "extra_env", "extra_secret", "kind"):
            changed = deepcopy(app)
            secrets = changed["properties"]["configuration"]["secrets"]
            rows = changed["properties"]["template"]["containers"][0]["env"]
            ref = next(row for row in rows if "secretRef" in row)
            if fault == "count":
                secrets.pop()
            elif fault == "name":
                secrets[0]["name"] = "other-secret"
            elif fault == "ref":
                ref["secretRef"] = "other-secret"
            elif fault == "value_swap":
                ref["value"] = ref.pop("secretRef")
            elif fault == "extra_env":
                rows.append({"name": "EXTRA", "value": "public"})
            elif fault == "extra_secret":
                secrets[0]["unexpected"] = "not-metadata"
            else:
                secrets[0] = {"name": "*******", "keyVaultUrl": "*******", "identity": "*******"}
            with self.subTest(fault=fault), self.assertRaises(metadata.MetadataError):
                metadata.validate_native_metadata(changed, descriptor)

    def test_post_create_requires_exact_unmasked_metadata(self):
        _, _, _, descriptor = self.descriptor()
        live = live_snapshot(descriptor)
        metadata.validate_live_metadata(live, descriptor, NEW)
        for fault in ("name", "masked", "ref", "value_swap", "target", "kind", "identity", "missing"):
            changed = deepcopy(live)
            if fault == "name":
                changed["secrets"][0]["name"] = "other-secret"
            elif fault == "masked":
                changed["secrets"][0]["name"] = "*******"
            elif fault == "ref":
                changed["containers"][0]["env"][0]["secretRef"] = "other-secret"
            elif fault == "value_swap":
                changed["containers"][0]["env"][0].update(hasRef=False, hasValue=True)
            elif fault == "target":
                changed["id"] = OLD
            elif fault == "kind":
                changed["secrets"][0].update(keyVaultUrl="https://synthetic.vault.azure.net/secrets/other", identity=IDENTITY)
            elif fault == "identity":
                changed["secrets"][0]["identity"] = IDENTITY
            else:
                changed["containers"][0]["env"].pop()
            with self.subTest(fault=fault), self.assertRaises(metadata.MetadataError):
                metadata.validate_live_metadata(changed, descriptor, NEW)

    def test_projection_does_not_fill_masks_and_empty_values_are_proven(self):
        _, _, _, descriptor = self.descriptor()
        app = native_app(descriptor)
        projected = metadata.project_proven_metadata(app, descriptor)
        self.assertEqual(projected["properties"]["configuration"]["secrets"], [])
        self.assertTrue(all("secretRef" not in row for row in projected["properties"]["template"]["containers"][0]["env"]))
        self.assertTrue(all(row["name"] == "*******" for row in app["properties"]["configuration"]["secrets"]))
        name = descriptor["emptyEnvValues"][0]
        row = next(row for row in app["properties"]["template"]["containers"][0]["env"] if row["name"] == name)
        row["value"] = "changed"
        with self.assertRaises(metadata.MetadataError):
            metadata.project_proven_metadata(app, descriptor)
