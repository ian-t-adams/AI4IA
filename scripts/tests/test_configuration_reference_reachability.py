"""Every azd/CI variable the operator docs name must be able to reach a deploy.

A repo variable only influences a deployment if BOTH halves hold:

1. ``.github/workflows/deploy.yml`` forwards it into the azd environment
   (``AI4IA_X: ${{ vars.AI4IA_X }}``), because CI cannot run ``azd env set``; and
2. ``infra/main.parameters.json`` reads it via a ``${AI4IA_X=default}`` token.

Miss either half and the failure is **silent in the worst possible way**: the
operator sets the variable, the deploy succeeds, and the value is ignored. That
is the same "configured but inert" class that has repeatedly bitten this repo,
and documentation is where it does the most damage, because a doc is what a
clean-room tenant standup follows.

This guards the *documentation* side. Two sibling tests guard the other sides:

* ``test_every_azd_parameter_token_is_reachable_from_ci`` (app/api tests) —
  every token in the parameter file is exported by CI.
* ``test_no_azd_parameter_export_shadows_its_parameter_file_default`` — an
  export must not carry a ``|| 'fallback'``, which makes the parameter default
  dead code.

Scope note: only ``docs/configuration-reference.md`` is checked, and only its
"azd / CI variable" column. ``docs/runbooks/feature-enablement.md`` has a
similarly-shaped table whose first column is "API flag / setting" — deployed
container env, a *different* namespace — so auditing it here would report
correct rows as lies.
"""

from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOY = ROOT / ".github" / "workflows" / "deploy.yml"
PARAMS = ROOT / "infra" / "main.parameters.json"
DOC = ROOT / "docs" / "configuration-reference.md"

# Column index (0-based) of "azd / CI variable" in the doc's tables.
AZD_COLUMN = 1

# Phrase the doc uses for a flag that is a literal in main.parameters.json and
# therefore deliberately NOT settable as a repo variable.
CHECKED_IN = "checked-in parameter"


def _forwarded() -> set[str]:
    """AI4IA_* variables deploy.yml exports into the azd environment."""
    text = DEPLOY.read_text(encoding="utf-8")
    return set(re.findall(r"^\s*(AI4IA_[A-Z0-9_]+):\s*\$\{\{", text, re.M))


def _consumed() -> set[str]:
    """AI4IA_* variables main.parameters.json reads via a ${...} token."""
    text = PARAMS.read_text(encoding="utf-8")
    return set(re.findall(r"\$\{(AI4IA_[A-Z0-9_]+)", text))


def _doc_claims() -> list[tuple[int, str, str]]:
    """(line number, feature name, variable) for each azd variable the doc names."""
    claims: list[tuple[int, str, str]] = []
    for n, line in enumerate(DOC.read_text(encoding="utf-8").splitlines(), 1):
        if not line.startswith("|") or line.startswith("| ---"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) <= AZD_COLUMN:
            continue
        cell = cells[AZD_COLUMN]
        if CHECKED_IN in cell:
            continue
        for token in re.findall(r"`(AI4IA_[A-Z0-9_]+)`", cell):
            claims.append((n, cells[0], token))
    return claims


class ConfigurationReferenceReachability(unittest.TestCase):
    def test_every_documented_azd_variable_can_reach_a_deploy(self) -> None:
        forwarded, consumed = _forwarded(), _consumed()
        broken: list[str] = []
        for line_no, feature, token in _doc_claims():
            reasons = []
            if token not in forwarded:
                reasons.append("not forwarded by deploy.yml")
            if token not in consumed:
                reasons.append("no ${...} token in main.parameters.json")
            if reasons:
                broken.append(
                    f"{DOC.name}:{line_no} ({feature}) documents {token} "
                    f"but it is {' and '.join(reasons)}. Either plumb it through "
                    f"both halves, or describe it as a '{CHECKED_IN}' the way the "
                    f"other literal flags in this table already are."
                )
        self.assertEqual([], broken, "\n" + "\n".join(broken))

    def test_every_settable_parameter_token_is_documented_somewhere(self) -> None:
        """A knob an operator cannot discover is nearly as bad as an inert one.

        AI4IA_APP_ENVIRONMENT was set to `prod` in the live repo while appearing
        in no tracked document, so a clean-room standup that set every
        *documented* variable would have silently deployed as `dev`.
        """
        docs = sorted((ROOT / "docs").rglob("*.md"))
        docs += [ROOT / "README.md", ROOT / "AGENTS.md"]
        blob = "\n".join(p.read_text(encoding="utf-8") for p in docs if p.is_file())
        self.assertGreater(len(docs), 10, "doc discovery found too few files")

        undocumented = sorted(t for t in _consumed() if t not in blob)
        self.assertEqual(
            [],
            undocumented,
            "\nThese azd variables influence a deploy but appear in no tracked "
            "doc, so an operator standing up a new tenant cannot discover them:\n"
            + "\n".join(f"  {t}" for t in undocumented),
        )

    def test_the_audit_is_not_vacuous(self) -> None:
        """A discovery step that stops matching looks exactly like a clean pass.

        Every assertion above is 'no problems found', which is also what a broken
        parser reports. These floors fail loudly if the inputs stop being read.
        """
        self.assertTrue(DEPLOY.is_file(), "deploy.yml not found")
        self.assertTrue(PARAMS.is_file(), "main.parameters.json not found")
        self.assertTrue(DOC.is_file(), "configuration-reference.md not found")
        self.assertGreater(len(_forwarded()), 30, "deploy.yml export scan found too few")
        self.assertGreater(len(_consumed()), 30, "parameter token scan found too few")
        self.assertGreater(
            len(_doc_claims()), 10, "doc table scan found too few azd claims"
        )
        self.assertIn(
            CHECKED_IN,
            DOC.read_text(encoding="utf-8"),
            f"the doc no longer uses the phrase '{CHECKED_IN}', so rows this test "
            "skips may no longer be the ones it means to skip",
        )


if __name__ == "__main__":
    unittest.main()
