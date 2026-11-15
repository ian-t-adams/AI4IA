"""Strict, default-compatible model provisioning catalog contract."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts._model_targets import provisioning_enabled

ROOT = Path(__file__).resolve().parents[2]


class ModelProvisioningTests(unittest.TestCase):
    def test_synthetic_catalog_rows_default_to_provisioning(self) -> None:
        rows = [
            {"name": "legacy-row"},
            {"name": "explicit-enabled", "provisioningEnabled": True},
            {"name": "opted-out", "provisioningEnabled": False},
        ]
        self.assertEqual(
            [row["name"] for row in rows if provisioning_enabled(row)],
            ["legacy-row", "explicit-enabled"],
        )

    def test_malformed_provisioning_values_fail_closed(self) -> None:
        for value in (None, 0, 1, "false", "true", [], {}, 1.0):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "provisioningEnabled must be a Boolean"
            ):
                provisioning_enabled({"provisioningEnabled": value})

    def test_actual_catalog_validator_rejects_malformed_values(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "provisioning_catalog_validator", ROOT / "scripts" / "validate-catalog.py"
        )
        assert spec is not None and spec.loader is not None
        validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(validator)
        original = json.loads((ROOT / "infra" / "models.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            for target in ("source", "external-claude"):
                index = next(
                    i for i, row in enumerate(original["catalog"])
                    if row.get("deploymentTarget", "source") == target
                )
                for value in ("omitted", True, False, None, "true", "false", 0, 1, 1.0, [], {}):
                    with self.subTest(target=target, value=value):
                        data = json.loads(json.dumps(original))
                        if value != "omitted":
                            data["catalog"][index]["provisioningEnabled"] = value
                        path.write_text(json.dumps(data), encoding="utf-8")
                        output = io.StringIO()
                        with patch.object(validator, "MODELS", path), contextlib.redirect_stdout(output):
                            result = validator.main()
                        valid = value == "omitted" or type(value) is bool
                        self.assertEqual(result, 0 if valid else 1, output.getvalue())
                        if not valid:
                            self.assertIn("provisioningEnabled must be a Boolean", output.getvalue())


if __name__ == "__main__":
    unittest.main()
