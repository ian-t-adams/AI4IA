"""Pins the Entra app-registration bootstrap against silent failure.

``provision-entra-apps.ps1`` is the documented one-command way to create the two
tenant objects production sign-in needs (the API audience and the web SPA client).
It runs exactly once per tenant, by hand, and is therefore never exercised by CI —
which is precisely how it shipped broken.

The original bug, found while standing up a new tenant: the script created the SPA
with ``az ad app create --spa-redirect-uris``. **That flag does not exist.** The
Azure CLI only offers ``--web-redirect-uris`` and ``--public-client-redirect-uris``;
``spa.redirectUris`` is settable through Microsoft Graph alone. So the CLI exited 2
with "unrecognized arguments", ``2>$null`` swallowed it, ``ConvertFrom-Json`` of an
empty string yielded ``$null``, and the script cheerfully printed
"Created web SPA app " (blank id), "Done", and a repo-variable table in which
``AI4IA_ENTRA_WEB_CLIENT_ID`` was still the literal ``<created-on-apply>``.

Nothing failed. Nothing was created. An operator following the runbook would have
pasted a placeholder into a repo variable and discovered it only as browser sign-in
failures after a full deploy.

Three properties keep that from recurring, and each is checked below:

1. No script or doc offers the nonexistent CLI flag. It is not a typo that a
   reviewer would catch — it reads exactly like the real ``--web-redirect-uris``.
2. Every app-creation path asserts it got an ``appId`` back and throws otherwise,
   so a Graph/CLI failure stops the run instead of poisoning the printed variables.
3. Admin consent reports failure. Granting it needs a *directory* role
   (Privileged Role Administrator / Cloud Application Administrator / Global
   Administrator); subscription **Owner is not enough**, which is the normal posture
   in MCAPS-managed tenants. The script must not claim consent it did not get.

Stdlib only and text-based on purpose: actually running the script would mutate a
real tenant, so the contract is enforced against the source text.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "provision-entra-apps.ps1"
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "deployment.md"

# `az ad app create` accepts neither of these. They are invented spellings that look
# plausible next to the real `--web-redirect-uris`.
NONEXISTENT_CLI_FLAGS = ("--spa-redirect-uris", "--spa-redirect-uri")


class TestNoNonexistentCliFlag(unittest.TestCase):
    def test_scripts_do_not_use_a_nonexistent_spa_flag(self) -> None:
        offenders: list[str] = []
        for path in sorted(REPO_ROOT.joinpath("scripts").rglob("*.ps1")):
            text = path.read_text(encoding="utf-8")
            for flag in NONEXISTENT_CLI_FLAGS:
                if flag in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)} uses {flag}")
        self.assertEqual(
            [],
            offenders,
            "`az ad app create` has no SPA redirect flag; set spa.redirectUris via "
            "Microsoft Graph instead. Offenders: " + "; ".join(offenders),
        )

    def test_runbook_does_not_document_a_nonexistent_spa_flag(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")
        for flag in NONEXISTENT_CLI_FLAGS:
            self.assertNotIn(
                flag,
                text,
                f"deployment.md documents {flag}, which az does not accept. The runbook "
                "is copy-pasted during a tenant standup, so a bad flag there fails the "
                "same way the script did.",
            )


class TestCreationFailsLoudly(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_api_and_web_creation_both_assert_an_app_id(self) -> None:
        for var, label in (("api", "API"), ("web", "web SPA")):
            pattern = rf"if \(-not \${var}\.appId\)\s*\{{\s*throw"
            # Checked with `search` rather than assertRegex so the failure message stays
            # readable -- assertRegex dumps the entire script into the report.
            self.assertIsNotNone(
                re.search(pattern, self.text),
                f"The {label} app creation path must throw when no appId comes back. "
                "Without it a failed create prints success and emits a placeholder "
                f"repo-variable value. Expected to find: {pattern}",
            )

    def test_app_creation_does_not_swallow_stderr(self) -> None:
        # `2>$null` on a create call is what hid the original failure. Reads elsewhere
        # (existence probes, best-effort sp create) may still silence expected noise.
        for line in self.text.splitlines():
            stripped = line.strip()
            if "az ad app create" in stripped:
                self.assertNotIn(
                    "2>$null",
                    stripped,
                    "Do not discard stderr on an app-creation call; that is exactly how "
                    f"the SPA bug stayed invisible. Offending line: {stripped}",
                )

    def test_spa_redirect_uris_are_set_through_graph(self) -> None:
        self.assertRegex(
            self.text,
            r"spa\s*=\s*@\{\s*redirectUris",
            "The SPA app must be created/patched with spa.redirectUris via Graph, which "
            "is the only supported way to set single-page-application redirect URIs.",
        )


class TestAdminConsentHonesty(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_consent_failure_is_surfaced(self) -> None:
        self.assertRegex(
            self.text,
            r"admin-consent[\s\S]{0,400}?Write-Warning",
            "Admin consent needs a directory role that subscription Owner does not "
            "confer, so it fails routinely. The script must warn instead of reporting "
            "a grant it never received.",
        )

    def test_consent_success_message_is_conditional(self) -> None:
        # The success line must sit inside a branch, not run unconditionally after the
        # best-effort `az ad app permission admin-consent` call.
        match = re.search(
            r"az ad app permission admin-consent[^\n]*\n([\s\S]{0,300})", self.text
        )
        self.assertIsNotNone(match, "expected an admin-consent invocation in the script")
        following = match.group(1)  # type: ignore[union-attr]
        grant_idx = following.find("Granted the web SPA")
        self.assertGreaterEqual(grant_idx, 0, "expected a consent success message")
        self.assertIn(
            "$LASTEXITCODE",
            following[:grant_idx],
            "The 'Granted ... with admin consent' message must be gated on the exit "
            "code of the admin-consent call.",
        )


if __name__ == "__main__":
    unittest.main()
