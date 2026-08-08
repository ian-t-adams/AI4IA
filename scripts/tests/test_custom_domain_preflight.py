"""Behavioural tests for deploy.yml's custom-domain preflight step.

The step is the only thing standing between an operator and two expensive
failure modes, and neither is visible to bicep:

* Provisioning with an empty ``AI4IA_*_CUSTOM_DOMAIN`` **wipes** a live vanity
  hostname, because bicep renders ``customDomains: []`` and ARM applies it.
* Binding a hostname for the first time cannot converge in a single pass.
  Container Apps refuses to create a managed certificate whose subject is not
  already a custom hostname in the environment
  (``RequireCustomHostnameInEnvironment``), and ARM creates the certificate
  before the ingress that would introduce it. Left alone, the run dies ~20
  minutes in, after the Foundry accounts and gateway are already built.

Asserting on the YAML text would only prove the script *mentions* the right
commands. These tests extract the real ``run:`` block and execute it under bash
with ``az`` stubbed, so the branch logic itself is covered.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/deploy.yml"


def _find_bash() -> str | None:
    found = shutil.which("bash")
    # The WSL shim on Windows agents is named bash.exe but cannot run anything
    # without a distro installed, so prefer a real Git Bash when one exists.
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    if found and os.name != "nt":
        return found
    return None


BASH = _find_bash()


def _preflight_script() -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = [
        step
        for step in workflow["jobs"]["deploy"]["steps"]
        if "custom-domain" in step.get("name", "").lower()
    ]
    if len(steps) != 1:
        raise AssertionError(
            f"expected exactly one custom-domain preflight step, found {len(steps)}"
        )
    return steps[0]["run"]


# Stub `az`. Records every invocation, and mimics the two output shapes the step
# depends on: `show -o none` exits non-zero for a missing app, and the
# customDomains tsv query prints nothing when an existing app has no domains.
# Pure shell on purpose -- the CI job that runs this has no interpreter beyond
# the one running unittest, and the fixture is a directory rather than JSON so
# the stub needs no parser.
AZ_STUB = """#!/usr/bin/env bash
echo "$*" >> "$AZ_CALLS_FILE"

app=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-n" ]; then app="$arg"; fi
  prev="$arg"
done

case "$*" in
  *"hostname add"*)
    if [ -e "$AZ_STATE_DIR/dns_fail" ]; then
      echo "ERROR: Failed to validate domain ownership" >&2
      exit 1
    fi
    exit 0
    ;;
  *"-o none"*)
    if [ -f "$AZ_STATE_DIR/apps/$app" ]; then exit 0; fi
    echo "ERROR: app not found" >&2
    exit 1
    ;;
  *customDomains*)
    if [ ! -f "$AZ_STATE_DIR/apps/$app" ]; then
      echo "ERROR: app not found" >&2
      exit 1
    fi
    cat "$AZ_STATE_DIR/apps/$app"
    exit 0
    ;;
esac
exit 0
"""


@unittest.skipIf(BASH is None, "bash is unavailable on this machine")
class CustomDomainPreflightTests(unittest.TestCase):
    """Drive the real step script through every branch that matters."""

    def run_preflight(
        self,
        *,
        apps: dict[str, list[str]],
        web_var: str = "",
        proxy_var: str = "",
        dns_ok: bool = True,
    ) -> tuple[int, str, list[str]]:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            statedir = tmpdir / "state"
            (statedir / "apps").mkdir(parents=True)
            for name, domains in apps.items():
                (statedir / "apps" / name).write_text(
                    "".join(f"{d}\n" for d in domains), newline="\n"
                )
            if not dns_ok:
                (statedir / "dns_fail").write_text("")
            calls = tmpdir / "calls.txt"
            calls.write_text("")

            bindir = tmpdir / "bin"
            bindir.mkdir()
            az = bindir / "az"
            az.write_text(AZ_STUB.replace("\r\n", "\n"), newline="\n")
            az.chmod(0o755)

            script = tmpdir / "step.sh"
            script.write_text(_preflight_script().replace("\r\n", "\n"), newline="\n")

            def posix(p: Path) -> str:
                s = str(p).replace("\\", "/")
                # Git Bash needs /c/... rather than C:/... for PATH entries.
                if len(s) > 1 and s[1] == ":":
                    s = "/" + s[0].lower() + s[2:]
                return s

            env = dict(os.environ)
            env["AZURE_ENV_NAME"] = "slurmfactory"
            env["AI4IA_WEB_CUSTOM_DOMAIN"] = web_var
            env["AI4IA_PROXY_CUSTOM_DOMAIN"] = proxy_var
            env["AZ_STATE_DIR"] = posix(statedir)
            env["AZ_CALLS_FILE"] = posix(calls)
            env["PATH"] = posix(bindir) + ":" + env.get("PATH", "")

            proc = subprocess.run(
                [BASH, str(script)],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            return (
                proc.returncode,
                proc.stdout + proc.stderr,
                calls.read_text().splitlines(),
            )

    # -- the wipe guard -------------------------------------------------

    def test_bound_domain_with_empty_variable_fails(self) -> None:
        """The original reason this step exists: silent destruction of a binding."""
        code, out, calls = self.run_preflight(
            apps={
                "ca-web-slurmfactory": ["ai4ia.nomad-analytics.com"],
                "ca-proxy-slurmfactory": [],
            },
        )
        self.assertEqual(code, 1, out)
        self.assertIn("would WIPE the binding", out)
        self.assertFalse([c for c in calls if "hostname add" in c])

    # -- the first-bind bootstrap ---------------------------------------

    def test_first_bind_registers_the_hostname(self) -> None:
        code, out, calls = self.run_preflight(
            apps={"ca-web-slurmfactory": [], "ca-proxy-slurmfactory": []},
            web_var="ai4ia.nomad-analytics.com",
        )
        self.assertEqual(code, 0, out)
        added = [c for c in calls if "hostname add" in c]
        self.assertEqual(len(added), 1, f"expected one registration, got {added}")
        self.assertIn("ai4ia.nomad-analytics.com", added[0])
        self.assertIn("ca-web-slurmfactory", added[0])

    def test_already_registered_hostname_is_not_re_added(self) -> None:
        """Resume path: phase one already happened, possibly still bindingType Disabled."""
        code, out, calls = self.run_preflight(
            apps={
                "ca-web-slurmfactory": ["ai4ia.nomad-analytics.com"],
                "ca-proxy-slurmfactory": [],
            },
            web_var="ai4ia.nomad-analytics.com",
        )
        self.assertEqual(code, 0, out)
        self.assertFalse([c for c in calls if "hostname add" in c])
        self.assertIn("already has ai4ia.nomad-analytics.com", out)

    def test_renaming_the_vanity_host_registers_the_new_one(self) -> None:
        """A *different* bound domain must not be mistaken for this one being ready."""
        code, out, calls = self.run_preflight(
            apps={
                "ca-web-slurmfactory": ["old.nomad-analytics.com"],
                "ca-proxy-slurmfactory": [],
            },
            web_var="ai4ia.nomad-analytics.com",
        )
        self.assertEqual(code, 0, out)
        added = [c for c in calls if "hostname add" in c]
        self.assertEqual(len(added), 1, f"expected one registration, got {added}")
        self.assertIn("ai4ia.nomad-analytics.com", added[0])

    def test_bad_dns_fails_fast_with_an_actionable_message(self) -> None:
        """This is the whole point: seconds, not 20 minutes into provisioning."""
        code, out, _ = self.run_preflight(
            apps={"ca-web-slurmfactory": [], "ca-proxy-slurmfactory": []},
            web_var="ai4ia.nomad-analytics.com",
            dns_ok=False,
        )
        self.assertEqual(code, 1, out)
        self.assertIn("CNAME", out)
        self.assertIn("asuid.ai4ia.nomad-analytics.com", out)

    # -- greenfield ordering ---------------------------------------------

    def test_greenfield_with_domain_set_is_rejected(self) -> None:
        """Step 3a's wrong order, caught before anything is built."""
        code, out, calls = self.run_preflight(
            apps={},
            web_var="ai4ia.nomad-analytics.com",
        )
        self.assertEqual(code, 1, out)
        self.assertIn("wrong order", out)
        self.assertFalse([c for c in calls if "hostname add" in c])

    def test_greenfield_with_domains_empty_passes(self) -> None:
        code, out, calls = self.run_preflight(apps={})
        self.assertEqual(code, 0, out)
        self.assertFalse([c for c in calls if "hostname add" in c])

    def test_steady_state_no_domains_configured_passes(self) -> None:
        code, out, calls = self.run_preflight(
            apps={"ca-web-slurmfactory": [], "ca-proxy-slurmfactory": []},
        )
        self.assertEqual(code, 0, out)
        self.assertFalse([c for c in calls if "hostname add" in c])

    def test_both_apps_bind_independently(self) -> None:
        code, out, calls = self.run_preflight(
            apps={"ca-web-slurmfactory": [], "ca-proxy-slurmfactory": []},
            web_var="ai4ia.nomad-analytics.com",
            proxy_var="genaiproxy.nomad-analytics.com",
        )
        self.assertEqual(code, 0, out)
        added = [c for c in calls if "hostname add" in c]
        self.assertEqual(len(added), 2, f"expected two registrations, got {added}")


class CustomDomainPreflightDocsTests(unittest.TestCase):
    """The runbook has to describe the sequence the workflow actually performs."""

    def test_runbook_explains_the_two_phase_bind(self) -> None:
        runbook = (ROOT / "docs/runbooks/greenfield-standup.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("RequireCustomHostnameInEnvironment", runbook)
        self.assertIn("az containerapp hostname add", runbook)

    def test_workflow_failures_point_to_the_greenfield_sequence(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "docs/runbooks/greenfield-standup.md section 6.2",
            workflow,
        )
        self.assertNotIn(
            "docs/runbooks/deployment.md section 3 step 3a",
            workflow,
        )

    def test_step_still_runs_before_provisioning(self) -> None:
        """Ordering is the fix. Registering after `azd provision` would be useless."""
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        names = [s.get("name", "") for s in workflow["jobs"]["deploy"]["steps"]]
        preflight = next(i for i, n in enumerate(names) if "custom-domain" in n.lower())
        provision = next(i for i, n in enumerate(names) if "Provision" in n)
        self.assertLess(
            preflight,
            provision,
            textwrap.dedent(
                """
                The custom-domain preflight must run before `azd provision`: it
                registers the hostname that the managed certificate needs to
                already exist.
                """
            ).strip(),
        )


if __name__ == "__main__":
    unittest.main()
