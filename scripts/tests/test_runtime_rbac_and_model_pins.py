"""Least-privilege runtime RBAC and explicit model-version posture."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class RuntimeRbacTests(unittest.TestCase):
    def test_key_vault_and_app_config_do_not_share_one_principal_list(self) -> None:
        module = (REPO / "infra" / "modules" / "keyvault.bicep").read_text(encoding="utf-8")
        main = (REPO / "infra" / "main.bicep").read_text(encoding="utf-8")
        self.assertNotIn("readerPrincipalIds", module)
        self.assertIn("keyVaultReaderPrincipalIds array", module)
        self.assertIn("appConfigReaderPrincipalIds array", module)
        self.assertRegex(main, r"keyVaultReaderPrincipalIds:\s*\[\]")
        self.assertRegex(
            main,
            r"appConfigReaderPrincipalIds:\s*\[\s*proxyIdentity\.principalId\s*\]",
        )
        # The public web identity must not be threaded into either data plane.
        keyvault_call = re.search(
            r"module keyvault .*?\n\}", main, re.S
        )
        self.assertIsNotNone(keyvault_call)
        assert keyvault_call is not None
        self.assertNotIn("webIdentity.principalId", keyvault_call.group(0))


class ModelVersionPinTests(unittest.TestCase):
    def test_deployments_never_auto_upgrade_away_from_the_catalog(self) -> None:
        module = (REPO / "infra" / "modules" / "models.bicep").read_text(encoding="utf-8")
        self.assertIn("version: d.version", module)
        self.assertIn("versionUpgradeOption: 'NoAutoUpgrade'", module)
        self.assertNotIn("OnceNewDefaultVersionAvailable", module)


if __name__ == "__main__":
    unittest.main()
