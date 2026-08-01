"""``.env.example`` is the operator-facing name for every setting, so a typo in it
is a silent no-op rather than an error.

pydantic-settings ignores an environment variable that maps to no field. An
operator who copies a misspelled name out of ``.env.example`` therefore gets no
warning, no startup failure, and a feature that stays off — the worst possible
failure mode for a security- or capability-gating flag.

This happened: ``.env.example`` documented ``AI4IA_CODE_INTERPRETER_RAW_FILES``
while the field is ``code_interpreter_raw_files_enabled``, so raw-file compute
could not be turned on by following the documentation.
"""
from __future__ import annotations

import re
from pathlib import Path

from ai4ia_api.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"
# ``KEY=value`` at the start of a line, ignoring comments and blanks.
_ASSIGNMENT = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=")


def _documented_keys() -> list[str]:
    keys: list[str] = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = _ASSIGNMENT.match(line.strip())
        if match and match.group("key").startswith("AI4IA_"):
            keys.append(match.group("key"))
    return keys


def _settable_env_names() -> set[str]:
    prefix = Settings.model_config["env_prefix"]
    names: set[str] = set()
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias or field.alias
        names.add(str(alias).upper() if alias else f"{prefix}{name}".upper())
    return names


def test_every_documented_env_var_maps_to_a_real_setting() -> None:
    settable = _settable_env_names()
    orphans = sorted(k for k in _documented_keys() if k.upper() not in settable)
    assert not orphans, (
        "these AI4IA_* names appear in app/api/.env.example but match no Settings "
        f"field, so setting them does nothing: {orphans}"
    )


def test_env_example_covers_a_meaningful_share_of_settings() -> None:
    """Guard against the check above passing vacuously on an emptied file.

    Not every field is operator-tunable, so this is a floor rather than parity.
    """
    documented = _documented_keys()
    assert len(documented) > 50, f"only {len(documented)} AI4IA_* keys found"


def test_a_misspelled_env_var_is_silently_ignored() -> None:
    """The premise of this module: pydantic-settings does not reject unknown names."""
    base = dict(env="local", auth_provider="dev", allow_dev_auth=True)
    misspelled = Settings(
        _env_file=None,
        **base,  # type: ignore[arg-type]
        **{"AI4IA_CODE_INTERPRETER_RAW_FILES": "true"},  # type: ignore[arg-type]
    )
    assert misspelled.code_interpreter_raw_files_enabled is False
