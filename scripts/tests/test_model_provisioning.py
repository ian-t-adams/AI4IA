"""Strict, default-compatible model provisioning catalog contract."""
from __future__ import annotations

import unittest

from scripts._model_targets import provisioning_enabled


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
        for value in (None, 0, 1, "false", []):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "provisioningEnabled must be a Boolean"
            ):
                provisioning_enabled({"provisioningEnabled": value})


if __name__ == "__main__":
    unittest.main()
