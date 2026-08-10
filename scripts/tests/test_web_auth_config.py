"""Contracts for fail-closed web authentication deployment wiring."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB_BICEP = (ROOT / "infra" / "modules" / "web.bicep").read_text(encoding="utf-8")


def _assert_partial_entra_fails_closed(source: str) -> None:
    assert "var entraRequested = authProvider == 'entra'" in source
    assert "var entraEnv = entraRequested ? [" in source
    assert "name: 'WEB_AUTH_PROVIDER'" in source
    assert "value: 'entra'" in source
    for name in ("ENTRA_CLIENT_ID", "ENTRA_TENANT_ID", "ENTRA_API_SCOPE"):
        assert f"name: '{name}'" in source
    assert (
        "var injectDevUser = appEnvironment != 'prod' "
        "&& !empty(devUser) && !entraRequested"
    ) in source


class WebAuthConfigTests(unittest.TestCase):
    def test_partial_entra_configuration_reaches_the_fail_closed_web_screen(
        self,
    ) -> None:
        _assert_partial_entra_fails_closed(WEB_BICEP)

    def test_contract_detects_provider_and_dev_user_guard_mutations(self) -> None:
        mutations = (
            WEB_BICEP.replace(
                "var entraEnv = entraRequested ? [",
                "var entraEnv = entraReady ? [",
            ),
            WEB_BICEP.replace(
                "&& !empty(devUser) && !entraRequested",
                "&& !empty(devUser) && !entraReady",
            ),
        )
        for mutated in mutations:
            with self.subTest():
                with self.assertRaises(AssertionError):
                    _assert_partial_entra_fails_closed(mutated)


if __name__ == "__main__":
    unittest.main()
