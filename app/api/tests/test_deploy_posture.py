"""Pin what the stock deploy defaults produce, across all three layers.

Audit finding P1-3 was that a fresh ``azd up`` served publicly with dev auth.
Verifying that is not a one-file read, and getting it wrong is easy in *both*
directions -- this file exists because an attempt to "correct" the finding as
overstated was itself wrong.

Reading ``config.py`` alone says fail-closed: ``validate_runtime()`` refuses dev
auth unless ``dev_auth_permitted`` (``env == local or allow_dev_auth``), and
``allow_dev_auth`` defaults to ``False``. Constructing ``Settings()`` with no
environment reproduces that and looks reassuring. It was wrong, because infra
overrode the code default: ``apiAllowDevAuth`` defaulted to ``true`` in
``main.bicep``, was absent from ``main.parameters.json`` so the Bicep default
applied, and reached the container as ``AI4IA_ALLOW_DEV_AUTH``.

That default is now ``false``, and the parameter is a first-class azd variable so
a demo has a supported opt-in rather than needing a file edit. A stock deploy
refuses to start instead of serving with client-controlled identity.

These tests assert the **composition**, which is the part no single-layer test
covers -- ``test_auth_dev.py`` passes the values in by hand, so it stays green
regardless of what infra ships. They are written to fail if the posture changes
in either direction, so whoever changes it has to update the audit finding in the
same commit.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from ai4ia_api.config import AuthProviderKind, Environment, Settings

REPO_ROOT = Path(__file__).resolve().parents[3]
PARAMETERS = REPO_ROOT / "infra" / "main.parameters.json"
MAIN_BICEP = REPO_ROOT / "infra" / "main.bicep"
API_BICEP = REPO_ROOT / "infra" / "modules" / "api.bicep"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"

# ``"value": "${AI4IA_AUTH_PROVIDER=dev}"`` -> ("AI4IA_AUTH_PROVIDER", "dev")
_TOKEN = re.compile(r"\$\{(?P<var>[A-Z0-9_]+)=(?P<default>[^}]*)\}")


def _parameter_token(name: str) -> tuple[str, str]:
    raw = json.loads(PARAMETERS.read_text(encoding="utf-8"))
    entry = raw["parameters"][name]["value"]
    match = _TOKEN.fullmatch(str(entry).strip())
    assert match is not None, f"{name} is not a ${{VAR=default}} token: {entry!r}"
    return match.group("var"), match.group("default")


def _parameter_default(name: str) -> str:
    return _parameter_token(name)[1]


def _bicep_bool_param(source: Path, name: str) -> bool:
    text = source.read_text(encoding="utf-8")
    match = re.search(rf"^param\s+{re.escape(name)}\s+bool\s*=\s*(true|false)\s*$", text, re.M)
    assert match is not None, f"{name} is not a defaulted bool param in {source.name}"
    return match.group(1) == "true"


def test_stock_parameters_still_default_to_dev() -> None:
    """The premise: the *environment* is still non-prod by default.

    That is deliberate -- naming an environment ``prod`` should be a choice. It
    is only dangerous when combined with permitted dev auth, which is what the
    assertions below now prevent.
    """
    assert _parameter_default("appEnvironment") == "dev"
    assert _parameter_default("apiAuthProvider") == "dev"


def test_dev_auth_is_off_by_default_and_opt_in_through_azd() -> None:
    """Secure by default, with a supported way to opt in.

    Both halves matter. A default of ``false`` with no azd variable would force a
    demo operator to edit ``main.parameters.json``, and an edited file is a
    change that gets committed by accident. A variable with a ``true`` default
    would be no fix at all.
    """
    assert _bicep_bool_param(MAIN_BICEP, "apiAllowDevAuth") is False
    variable, default = _parameter_token("apiAllowDevAuth")
    assert variable == "AI4IA_ALLOW_DEV_AUTH"
    assert default == "false"


def test_the_azd_variable_is_actually_forwarded_by_the_deploy_workflow() -> None:
    """A variable only influences a deploy if BOTH halves are wired.

    ``main.parameters.json`` must read it and ``deploy.yml`` must forward it.
    Miss either and the operator sets it, the deploy goes green, and nothing
    changes -- the exact failure ``test_configuration_reference_reachability``
    was written for.
    """
    assert "AI4IA_ALLOW_DEV_AUTH: ${{ vars.AI4IA_ALLOW_DEV_AUTH }}" in DEPLOY_WORKFLOW.read_text(
        encoding="utf-8"
    )


def test_prod_still_forces_dev_auth_off_regardless_of_the_flag() -> None:
    """Belt and braces: even an operator who sets the variable cannot get dev
    auth in a ``prod`` environment."""
    assert re.search(
        r"allowDevAuth:\s*appEnvironment\s*==\s*'prod'\s*\?\s*false\s*:\s*apiAllowDevAuth",
        MAIN_BICEP.read_text(encoding="utf-8"),
    ), "main.bicep no longer forces allowDevAuth false in prod"
    assert "AI4IA_ALLOW_DEV_AUTH" in API_BICEP.read_text(encoding="utf-8"), (
        "api.bicep no longer injects AI4IA_ALLOW_DEV_AUTH; the chain these tests "
        "describe has changed and the P1-3 finding needs re-verifying"
    )


def _settings_from_stock_deploy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    app_environment: str,
    allow_dev_auth: bool | None = None,
) -> Settings:
    """``Settings`` as the deployed container would build it.

    Ambient ``AI4IA_*`` variables are cleared so this asserts the shipped
    defaults and not the developer's shell.
    """
    for key in list(os.environ):
        if key.startswith("AI4IA_"):
            monkeypatch.delenv(key, raising=False)
    if allow_dev_auth is None:
        allow_dev_auth = (
            False
            if app_environment == "prod"
            else _parameter_default("apiAllowDevAuth") == "true"
        )
    return Settings(
        env=Environment(app_environment),
        auth_provider=AuthProviderKind(_parameter_default("apiAuthProvider")),
        allow_dev_auth=allow_dev_auth,
    )


def test_stock_deploy_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """The finding, closed and executable.

    A clean-room ``azd up`` no longer serves with client-controlled identity. It
    fails at startup, loudly, naming both ways forward.
    """
    settings = _settings_from_stock_deploy(
        monkeypatch, app_environment=_parameter_default("appEnvironment")
    )

    assert settings.allow_dev_auth is False
    assert settings.dev_auth_permitted is False
    with pytest.raises(RuntimeError, match="Dev auth is disabled outside local"):
        settings.validate_runtime()


def test_a_demo_can_still_opt_in_deliberately(monkeypatch: pytest.MonkeyPatch) -> None:
    """The escape hatch still exists, and is still spoofable.

    Asserted rather than assumed so the cost of setting the variable stays
    visible in the test suite, not only in a runbook.
    """
    settings = _settings_from_stock_deploy(
        monkeypatch, app_environment="dev", allow_dev_auth=True
    )
    settings.validate_runtime()  # starts
    assert settings.auth_provider_is_spoofable is True  # and trusts X-Dev-User


def test_prod_refuses_dev_auth_even_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_from_stock_deploy(monkeypatch, app_environment="prod")
    assert settings.dev_auth_permitted is False
    with pytest.raises(RuntimeError, match="Dev auth is disabled outside local"):
        settings.validate_runtime()
