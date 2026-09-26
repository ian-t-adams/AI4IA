#!/usr/bin/env python3
"""Derive the azd transports of the JSON-valued deployment variables.

azd inserts environment values into infra/main.parameters.json without JSON
escaping, so AI4IA_CLAUDE_BINDING_JSON, AI4IA_GROUP_POLICY_JSON and the secret
AI4IA_PROXY_PROFILE_PROJECTION_JSON travel as <NAME>_B64 base64 transports
(scripts/_json_transport.py). The raw variables stay the operator contract; never
set a transport by hand.

--github-env  deploy.yml, before `azd provision`: append every transport to
              $GITHUB_ENV. A secret-derived value is masked with ::add-mask::
              before it is written anywhere.
--azd-env     First azd preprovision hook: store each transport that differs from
              the value azd would read in the active azd environment. Values go
              through a private dotenv file, never a command line. azd reloads its
              environment after every hook, before the next hook runs and before
              it resolves the parameters file.

Neither mode prints a transport value, except inside the mask command.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO

sys.path.insert(0, str(Path(__file__).parent))
from _json_transport import TRANSPORTS, TransportError, encode


def derived(environ: Mapping[str, str]) -> dict[str, str]:
    """Transport of every raw variable; an unset or empty variable yields ''."""
    values: dict[str, str] = {}
    for transport in TRANSPORTS:
        try:
            values[transport.transport_variable] = encode(environ.get(transport.variable, ""))
        except TransportError as exc:
            raise SystemExit(f"error: {transport.variable} cannot be carried: {exc}.") from None
    return values


def github_env(environ: Mapping[str, str], out: IO[str]) -> int:
    target = environ.get("GITHUB_ENV", "")
    if not target:
        print("error: --github-env requires GITHUB_ENV.", file=sys.stderr)
        return 2
    values = derived(environ)
    summary = []
    for transport in TRANSPORTS:
        value = values[transport.transport_variable]
        if transport.secret and value:
            # Before any write: later steps print $GITHUB_ENV values in their logs.
            out.write(f"::add-mask::{value}\n")
        state = ("set, masked" if transport.secret else "set") if value else "empty"
        summary.append(f"{transport.transport_variable} ({state})")
    out.flush()
    with open(target, "a", encoding="utf-8", newline="\n") as handle:
        for name, value in values.items():
            handle.write(f"{name}={value}\n")
    out.write("Derived azd transports: " + ", ".join(summary) + ".\n")
    return 0


def azd_env(
    environ: Mapping[str, str],
    out: IO[str],
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> int:
    # A hook process sees os.environ followed by the azd environment's values, the
    # same precedence azd uses to resolve ${NAME} (its .env first, then the OS).
    stale = {
        name: value for name, value in derived(environ).items()
        if environ.get(name, "") != value
    }
    if not stale:
        out.write("azd JSON transports are current.\n")
        return 0
    names = ", ".join(sorted(stale))
    azd = which("azd")
    if azd is None:
        print(f"error: azd is not on PATH, so {names} cannot be stored.", file=sys.stderr)
        return 1
    descriptor, path = tempfile.mkstemp(prefix="ai4ia-json-transport-", suffix=".env")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for name, value in stale.items():
                # Single quotes keep godotenv literal; base64 contains none.
                handle.write(f"{name}='{value}'\n")
        command = [azd, "env", "set", "--file", path]
        if environ.get("AZURE_ENV_NAME"):
            command += ["--environment", environ["AZURE_ENV_NAME"]]
        result = run(command, check=False)
    finally:
        os.unlink(path)
    if result.returncode != 0:
        print(f"error: `azd env set` exited {result.returncode}; {names} were not stored.", file=sys.stderr)
        return 1
    out.write(f"Stored azd transports: {names}.\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive the azd transports of the JSON-valued deployment variables."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--github-env", action="store_true", help="append to $GITHUB_ENV (deploy.yml)")
    mode.add_argument("--azd-env", action="store_true", help="store in the azd environment (preprovision hook)")
    args = parser.parse_args(argv)
    if args.github_env:
        return github_env(os.environ, sys.stdout)
    return azd_env(os.environ, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
