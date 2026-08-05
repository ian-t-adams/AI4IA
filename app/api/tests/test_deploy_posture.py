"""Pin what the stock deploy defaults actually produce, across all three layers.

Audit finding P1-3 says a fresh ``azd up`` serves publicly with dev auth. Verifying
that is not a one-file read, and getting it wrong is easy in *both* directions --
this file exists because an attempt to "correct" the finding as overstated was itself
wrong.

Reading ``config.py`` alone says fail-closed: ``validate_runtime()`` refuses dev auth
unless ``dev_auth_permitted`` (``env == local or allow_dev_auth``), and
``allow_dev_auth`` defaults to ``False``. Constructing ``Settings()`` with no
environment reproduces that and looks reassuring.

It is wrong, because infra overrides the code default:

* ``infra/main.bicep:70`` -- ``param apiAllowDevAuth bool = true``, absent from
  ``main.parameters.json``, so the Bicep default applies.
* ``infra/main.bicep:845`` -- ``allowDevAuth: appEnvironment == 'prod' ? false :
  apiAllowDevAuth``. ``appEnvironment`` defaults to ``dev``, so this is ``true``.
* ``infra/modules/api.bicep:731-732`` -- injected as ``AI4IA_ALLOW_DEV_AUTH``.

So the deployed container has ``allow_dev_auth=True``, ``validate_runtime()`` passes,
and identity comes from the client-supplied ``X-Dev-User`` header on public ingress.
``appEnvironment == 'prod'`` is the only thing that forces it closed, and ``prod`` is
not the default.

These tests assert the **composition**, which is the part no single-layer test covers
(`test_auth_dev.py` passes the values in by hand). They are written to fail if the
posture changes in either direction, so whoever changes it has to update the audit
finding in the same commit.
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

# ``"value": "${AI4IA_AUTH_PROVIDER=dev}"`` -> "dev"
_TOKEN = re.compile(r"\$\{(?P<var>[A-Z0-9_]+)=(?P<default>[^}]*)\}")


def _parameter_default(name: str) -> str:
    raw = json.loads(PARAMETERS.read_text(encoding="utf-8"))
    entry = raw["parameters"][name]["value"]
    match = _TOKEN.fullmatch(str(entry).strip())
    assert match is not None, f"{name} is not a ${{VAR=default}} token: {entry!r}"
    return match.group("default")


def _bicep_bool_param(source: Path, name: str) -> bool:
    text = source.read_text(encoding="utf-8")
    match = re.search(rf"^param\s+{re.escape(name)}\s+bool\s*=\s*(true|false)\s*$", text, re.M)
    assert match is not None, f"{name} is not a defaulted bool param in {source.name}"
    return match.group(1) == "true"


def test_stock_parameters_still_default_to_dev() -> None:
    """The premise. If these change, everything below stops meaning what it says."""
    assert _parameter_default("appEnvironment") == "dev"
    assert _parameter_default("apiAuthProvider") == "dev"


def test_allow_dev_auth_is_not_pinned_in_the_parameter_file() -> None:
    """Which is why the Bicep default is the value that actually ships."""
    raw = json.loads(PARAMETERS.read_text(encoding="utf-8"))
    assert "apiAllowDevAuth" not in raw["parameters"]


def test_the_bicep_default_is_what_makes_a_stock_deploy_serve() -> None:
    """``apiAllowDevAuth`` defaults true, and only ``prod`` overrides it.

    This is the single line that decides whether a stock deploy is reachable with
    spoofable identity. Asserted literally so a change to it cannot pass silently.
    """
    assert _bicep_bool_param(MAIN_BICEP, "apiAllowDevAuth") is True
    assert re.search(
        r"allowDevAuth:\s*appEnvironment\s*==\s*'prod'\s*\?\s*false\s*:\s*apiAllowDevAuth",
        MAIN_BICEP.read_text(encoding="utf-8"),
    ), "main.bicep no longer forces allowDevAuth false in prod"
    assert "AI4IA_ALLOW_DEV_AUTH" in API_BICEP.read_text(encoding="utf-8"), (
        "api.bicep no longer injects AI4IA_ALLOW_DEV_AUTH; the chain this test "
        "describes has changed and the P1-3 finding needs re-verifying"
    )


def _settings_from_stock_deploy(
    monkeypatch: pytest.MonkeyPatch, *, app_environment: str
) -> Settings:
    """``Settings`` as the deployed container would build it, for a given environment.

    Ambient ``AI4IA_*`` variables are cleared so this asserts the shipped defaults and
    not the developer's shell.
    """
    for key in list(os.environ):
        if key.startswith("AI4IA_"):
            monkeypatch.delenv(key, raising=False)
    allow_dev_auth = (
        False if app_environment == "prod" else _bicep_bool_param(MAIN_BICEP, "apiAllowDevAuth")
    )
    return Settings(
        env=Environment(app_environment),
        auth_provider=AuthProviderKind(_parameter_default("apiAuthProvider")),
        allow_dev_auth=allow_dev_auth,
    )


def test_stock_deploy_serves_with_client_controlled_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The finding, executable.

    Not a wish: this asserts the current, undesired behaviour on purpose, so that
    fixing P1-3 forces this test to be updated alongside the audit entry.
    """
    settings = _settings_from_stock_deploy(
        monkeypatch, app_environment=_parameter_default("appEnvironment")
    )

    assert settings.allow_dev_auth is True
    assert settings.dev_auth_permitted is True
    assert settings.auth_provider_is_spoofable is True
    settings.validate_runtime()  # does NOT raise -- the app starts and serves


def test_prod_is_the_only_thing_that_forces_it_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_from_stock_deploy(monkeypatch, app_environment="prod")

    assert settings.allow_dev_auth is False
    assert settings.dev_auth_permitted is False
    with pytest.raises(RuntimeError, match="Dev auth is disabled outside local"):
        settings.validate_runtime()
