"""deploy.yml must build each image once and deploy that exact digest (P1-7).

Before this, `azd deploy` rebuilt all three images from source at deploy time.
The artifact that ran in production was therefore never the artifact CI tested
-- a different build, at a different moment, and (until the base images were
digest-pinned) potentially on a different base image. Nothing recorded which
bytes were live, so "which commit is production running?" had no answer beyond
"whatever main was when the workflow ran".

The replacement builds each service once, reads back the digest the *registry*
assigned, and hands that reference to `azd deploy <service> --from-package`.
azd's own source is what makes that safe, and it is worth naming the exact
mechanism because the whole change rests on it: with `--from-package` set, azd
never calls its packager (`internal/cmd/service_graph.go`), and when the
reference carries a registry hostname the containerapp target skips ACR
login/tag/push and forwards the ORIGINAL string
(`pkg/project/service_target_containerapp.go`). A reference *without* a
registry silently falls back to azd building and pushing it -- which is why the
step asserts the login server has a dot, and why that assertion is tested here.

Asserting on the YAML text alone would only prove the step *mentions* the right
commands. So, following `test_custom_domain_preflight.py`, the real `run:` block
is extracted and executed under bash with `docker` and `az` stubbed. The static
half then guards the wiring that no stub can see: that every service in
azure.yaml is actually deployed, that nothing falls back to a rebuild, and that
the P1-6 capture/preflight/deploy/verify/rollback ordering still holds.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/deploy.yml"
AZURE_YAML = ROOT / "azure.yaml"

BUILD_STEP = "Build and push service images"
DEPLOY_STEP = "Deploy application"
VERIFY_STEP = "Verify the deploy actually landed"

# 64 lowercase hex, distinct per service so a crossed-wire mapping is visible.
DIGESTS = {
    "web-prod": "a" * 64,
    "api-prod": "b" * 64,
    "proxy-prod": "c" * 64,
}


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


def _steps() -> list[dict]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return list(document["jobs"]["deploy"]["steps"])


def _step(prefix: str) -> dict:
    matches = [s for s in _steps() if str(s.get("name", "")).startswith(prefix)]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {prefix!r} step, found {len(matches)}")
    return matches[0]


def _step_index(prefix: str) -> int:
    for index, step in enumerate(_steps()):
        if str(step.get("name", "")).startswith(prefix):
            return index
    raise AssertionError(f"no step named {prefix!r}")


def _services() -> list[str]:
    document = yaml.safe_load(AZURE_YAML.read_text(encoding="utf-8"))
    return list(document["services"])


DOCKER_STUB = """#!/usr/bin/env bash
echo "$*" >> "$DOCKER_CALLS"

if [ "$1 $2" = "image inspect" ]; then
  ref="${3%:*}"
  if [ -e "$STUB_DIR/inspect_fails" ]; then
    echo "Error: No such image" >&2
    exit 1
  fi
  if [ -e "$STUB_DIR/no_digests" ]; then
    exit 0
  fi
  if [ -e "$STUB_DIR/foreign_digest_first" ]; then
    echo "other.azurecr.io/somewhere/else@sha256:${STUB_FOREIGN}"
  fi
  digest_file="$STUB_DIR/digest_for_$(basename "$ref")"
  if [ -f "$digest_file" ]; then
    echo "${ref}@sha256:$(cat "$digest_file")"
  fi
  exit 0
fi

if [ "$1" = "build" ] && [ -e "$STUB_DIR/build_fails" ]; then
  echo "build failed" >&2
  exit 1
fi
exit 0
"""

AZ_STUB = """#!/usr/bin/env bash
echo "$*" >> "$AZ_CALLS"
case "$*" in
  *"acr list"*)
    cat "$STUB_DIR/registries"
    ;;
esac
exit 0
"""


@unittest.skipIf(BASH is None, "bash is unavailable on this machine")
class BuildAndPushStepTests(unittest.TestCase):
    """Drive the real build/push script through the branches that matter."""

    def run_step(
        self,
        *,
        registries: str = "crai4ia1234.azurecr.io",
        env_name: str = "prod",
        digests: dict[str, str] | None = None,
        flags: tuple[str, ...] = (),
    ) -> tuple[
        subprocess.CompletedProcess[str], dict[str, str], list[str], list[str], str
    ]:
        import tempfile

        digests = DIGESTS if digests is None else digests
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stub_dir = root / "stubs"
            stub_dir.mkdir()
            (stub_dir / "registries").write_text(registries + "\n", encoding="utf-8")
            for name, digest in digests.items():
                (stub_dir / f"digest_for_{name}").write_text(digest, encoding="utf-8")
            for flag in flags:
                (stub_dir / flag).write_text("", encoding="utf-8")

            bin_dir = root / "bin"
            bin_dir.mkdir()
            for name, body in (("docker", DOCKER_STUB), ("az", AZ_STUB)):
                target = bin_dir / name
                target.write_text(body, encoding="utf-8", newline="\n")
                target.chmod(0o755)

            output = root / "output"
            summary = root / "summary"
            docker_calls = root / "docker-calls"
            az_calls = root / "az-calls"
            for path in (output, summary, docker_calls, az_calls):
                path.write_text("", encoding="utf-8")

            script = root / "step.sh"
            script.write_text(_step(BUILD_STEP)["run"], encoding="utf-8", newline="\n")

            env = dict(os.environ)
            env.update(
                PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
                STUB_DIR=str(stub_dir),
                STUB_FOREIGN="f" * 64,
                DOCKER_CALLS=str(docker_calls),
                AZ_CALLS=str(az_calls),
                GITHUB_OUTPUT=str(output),
                GITHUB_STEP_SUMMARY=str(summary),
                GITHUB_SHA="1234567890abcdef1234567890abcdef12345678",
                AZURE_ENV_NAME=env_name,
                AI4IA_WORKLOAD="ai4ia",
            )
            assert BASH is not None
            result = subprocess.run(
                [BASH, str(script)],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(ROOT),
            )
            outputs = {}
            for line in output.read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                if key:
                    outputs[key] = value
            return (
                result,
                outputs,
                docker_calls.read_text(encoding="utf-8").splitlines(),
                az_calls.read_text(encoding="utf-8").splitlines(),
                summary.read_text(encoding="utf-8"),
            )

    def test_happy_path_emits_a_digest_reference_per_service(self) -> None:
        result, outputs, _, _, _ = self.run_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            outputs,
            {
                "web_image": f"crai4ia1234.azurecr.io/ai4ia/web-prod@sha256:{'a' * 64}",
                "api_image": f"crai4ia1234.azurecr.io/ai4ia/api-prod@sha256:{'b' * 64}",
                "proxy_image": (
                    f"crai4ia1234.azurecr.io/ai4ia/proxy-prod@sha256:{'c' * 64}"
                ),
            },
        )

    def test_it_builds_each_service_from_its_own_context(self) -> None:
        _, _, docker_calls, _, _ = self.run_step()
        builds = [call for call in docker_calls if call.startswith("build ")]
        self.assertEqual(len(builds), 3, docker_calls)
        for context in ("app/web", "app/api", "proxy"):
            self.assertTrue(
                any(f"--file {context}/Dockerfile" in call for call in builds),
                f"nothing built {context}/Dockerfile: {builds}",
            )

    def test_every_build_targets_the_platform_container_apps_runs(self) -> None:
        """An arm64 build provisions fine and then never starts."""

        _, _, docker_calls, _, _ = self.run_step()
        builds = [call for call in docker_calls if call.startswith("build ")]
        self.assertEqual(len(builds), 3, docker_calls)
        for call in builds:
            self.assertIn("--platform linux/amd64", call)

    def test_it_pushes_the_commit_tag_before_reading_the_digest(self) -> None:
        _, _, docker_calls, _, _ = self.run_step()
        sha = "1234567890abcdef1234567890abcdef12345678"
        for service in ("web", "api", "proxy"):
            ref = f"crai4ia1234.azurecr.io/ai4ia/{service}-prod"
            push = f"push {ref}:{sha}"
            inspect = f"image inspect {ref}:{sha}"
            self.assertIn(push, docker_calls)
            self.assertTrue(
                any(call.startswith(inspect) for call in docker_calls), docker_calls
            )
            self.assertLess(
                docker_calls.index(push),
                next(i for i, c in enumerate(docker_calls) if c.startswith(inspect)),
                "RepoDigests is only populated by the push, so reading it first "
                "would silently produce no digest",
            )

    def test_it_logs_into_the_registry_it_discovered(self) -> None:
        _, _, _, az_calls, _ = self.run_step()
        self.assertIn("acr login --name crai4ia1234", az_calls)

    def test_two_registries_fail_rather_than_guessing(self) -> None:
        result, outputs, _, _, _ = self.run_step(
            registries="crone.azurecr.io\ncrtwo.azurecr.io"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outputs, {})
        self.assertIn("Expected exactly one container registry", result.stdout)

    def test_no_registry_at_all_fails(self) -> None:
        result, _, _, _, _ = self.run_step(registries="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Expected exactly one container registry", result.stdout)

    def test_a_dotless_login_server_fails(self) -> None:
        """azd only skips its own build+push when it can see a registry host."""

        result, outputs, _, _, _ = self.run_step(registries="crai4ia1234")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outputs, {})
        self.assertIn("azd would silently rebuild", result.stdout)

    def test_a_missing_digest_fails_instead_of_deploying_the_tag(self) -> None:
        result, outputs, _, _, _ = self.run_step(flags=("no_digests",))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outputs, {})
        self.assertIn("Could not read a registry digest", result.stdout)

    def test_an_unreadable_image_fails(self) -> None:
        result, _, _, _, _ = self.run_step(flags=("inspect_fails",))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not read a registry digest", result.stdout)

    def test_a_failing_build_stops_the_run(self) -> None:
        result, outputs, _, _, _ = self.run_step(flags=("build_fails",))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outputs, {})

    def test_a_foreign_repo_digest_is_not_mistaken_for_ours(self) -> None:
        """An image can carry several RepoDigests; only one is this repository."""

        result, outputs, _, _, _ = self.run_step(flags=("foreign_digest_first",))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            outputs["web_image"],
            f"crai4ia1234.azurecr.io/ai4ia/web-prod@sha256:{'a' * 64}",
        )

    def test_the_repository_path_is_lowercased_like_azd_does(self) -> None:
        """azd's DefaultImageName lowercases the environment name."""

        result, outputs, _, _, _ = self.run_step(
            env_name="PROD",
            digests={"web-prod": "a" * 64, "api-prod": "b" * 64, "proxy-prod": "c" * 64},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(outputs["web_image"].startswith("crai4ia1234.azurecr.io/ai4ia/web-prod@"))

    def test_it_records_the_digests_where_an_operator_will_see_them(self) -> None:
        """P1-7 asks for a running revision to be traceable back to a commit."""

        result, outputs, _, _, summary = self.run_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1234567890abcdef1234567890abcdef12345678", summary)
        for service in ("web", "api", "proxy"):
            reference = outputs[f"{service}_image"]
            self.assertIn(reference, summary)
            self.assertIn(f"{service} -> {reference}", result.stdout)


class DeployWiringTests(unittest.TestCase):
    """The wiring around the step, which no stub can observe."""

    def test_every_azure_yaml_service_is_deployed_by_digest(self) -> None:
        run = _step(DEPLOY_STEP)["run"]
        deployed = set(re.findall(r"azd deploy (\S+) --from-package", run))
        self.assertEqual(
            deployed,
            set(_services()),
            "deploy.yml names services one at a time now (azd rejects "
            "--from-package with --all), so a service added to azure.yaml and "
            "not here would silently stop being deployed -- something the old "
            "`azd deploy --no-prompt` form could not do.",
        )

    def test_no_step_falls_back_to_a_rebuilding_deploy(self) -> None:
        for step in _steps():
            run = str(step.get("run") or "")
            for line in run.splitlines():
                stripped = line.strip()
                if not stripped.startswith("azd deploy"):
                    continue
                self.assertIn(
                    "--from-package",
                    stripped,
                    f"{step.get('name')!r} runs `{stripped}`, which makes azd "
                    "build the image from source at deploy time -- the exact "
                    "behaviour finding P1-7 removes.",
                )

    def test_the_build_uses_the_commit_sha_as_its_tag(self) -> None:
        run = _step(BUILD_STEP)["run"]
        self.assertIn("${GITHUB_SHA}", run)

    def test_images_are_built_before_the_rollback_capture(self) -> None:
        """A build failure must not fire the P1-6 rollback.

        The rollback is gated on `steps.capture.outcome == 'success'`, so
        capturing first would make every failed build "restore" revisions that
        were never touched.
        """

        self.assertLess(_step_index(BUILD_STEP), _step_index("Capture pre-deploy"))

    def test_the_p1_6_ordering_is_preserved(self) -> None:
        order = [
            "Capture pre-deploy",
            "Preflight the post-deploy canary token",
            DEPLOY_STEP,
            "Acquire the canary token",
            VERIFY_STEP,
            "Roll back to the captured revisions",
        ]
        indexes = [_step_index(name) for name in order]
        self.assertEqual(indexes, sorted(indexes), order)

    def test_the_rollback_guards_are_unchanged(self) -> None:
        condition = str(_step("Roll back to the captured revisions")["if"])
        for guard in (
            "failure()",
            "steps.capture.outcome == 'success'",
            "steps.canary_token.outcome != 'failure'",
        ):
            self.assertIn(guard, condition)

    def test_verification_asserts_the_exact_deployed_digest(self) -> None:
        run = _step(VERIFY_STEP)["run"]
        # Both branches -- the canary one and the --skip-canary one.
        self.assertEqual(run.count("post-deploy-verify.py verify"), 2)
        for service in _services():
            self.assertEqual(
                run.count(f'--expect-image "{service}='),
                2,
                f"{service} is not asserted in both verify branches. Without it "
                "the verifier falls back to 'the image string changed', which a "
                "content-addressed digest cannot guarantee -- a rebuild of the "
                "same content would roll back a healthy release.",
            )

    def test_the_deploy_and_verify_steps_read_the_build_step_outputs(self) -> None:
        for name in (DEPLOY_STEP, VERIFY_STEP):
            env = _step(name).get("env") or {}
            referenced = " ".join(str(value) for value in env.values())
            for service in _services():
                self.assertIn(
                    f"steps.images.outputs.{service}_image",
                    referenced,
                    f"{name} does not read the {service} digest",
                )


class ResourceGroupDerivationTests(unittest.TestCase):
    """The shell must derive the resource group the way the Bicep does.

    Two steps in deploy.yml build the RG name in bash: the custom-domain
    preflight and the image build/push step. Neither can ask Bicep, so both are
    hand-copies of `infra/main.bicep`, and a hand-copy that is merely *consistent
    with the other copy* is worthless -- if the original preflight were wrong,
    copying it would propagate the bug rather than reveal it.

    So the expected string is **derived** here from `infra/main.bicep` and
    `infra/main.parameters.json` rather than hardcoded: the RG template, the
    parameter order, the azd variable each parameter reads, and each default.
    Renaming the workload parameter, changing its default, or restructuring the
    RG name now fails this test instead of sending every `az` lookup in the
    deploy job to a resource group that does not exist -- which fails *silently*,
    because the preflight treats a missing app as "nothing bound".
    """

    def setUp(self) -> None:
        self.bicep = (ROOT / "infra/main.bicep").read_text(encoding="utf-8")
        self.parameters = json.loads(
            (ROOT / "infra/main.parameters.json").read_text(encoding="utf-8")
        )["parameters"]

    def _rg_template_parameters(self) -> list[str]:
        """The parameter names in `var resourceGroupName = 'rg-${a}-${b}'`."""

        match = re.search(
            r"var\s+resourceGroupName\s*=\s*'([^']+)'", self.bicep
        )
        self.assertIsNotNone(
            match, "infra/main.bicep no longer declares `var resourceGroupName`"
        )
        assert match is not None
        template = match.group(1)
        self.assertTrue(
            template.startswith("rg-"),
            f"resource group template changed shape: {template!r}",
        )
        return re.findall(r"\$\{(\w+)\}", template)

    def _shell_expansion_for(self, parameter: str) -> str:
        """`${AI4IA_WORKLOAD=ai4ia}` -> `${AI4IA_WORKLOAD:-ai4ia}`."""

        value = self.parameters[parameter]["value"]
        match = re.fullmatch(r"\$\{(\w+)(?:=([^}]*))?\}", value)
        self.assertIsNotNone(
            match, f"{parameter} is not a plain azd substitution: {value!r}"
        )
        assert match is not None
        variable, default = match.group(1), match.group(2)
        # azd's ${VAR=default} behaves like POSIX ${VAR:-default}.
        return f"${{{variable}:-{default}}}" if default is not None else f"${{{variable}}}"

    def _expected_rg_expression(self) -> str:
        names = self._rg_template_parameters()
        self.assertEqual(
            names,
            ["workload", "environmentName"],
            "the RG template's parameters changed; update the deploy.yml shell too",
        )
        return "rg-" + "-".join(self._shell_expansion_for(name) for name in names)

    def test_the_bicep_default_and_the_parameter_file_default_agree(self) -> None:
        """A split default would make the shell right for only one of them."""

        bicep_default = re.search(
            r"param\s+workload\s+string\s*=\s*'([^']+)'", self.bicep
        )
        self.assertIsNotNone(bicep_default, "main.bicep lost its workload default")
        assert bicep_default is not None
        shell = self._shell_expansion_for("workload")
        self.assertTrue(
            shell.endswith(f":-{bicep_default.group(1)}}}"),
            f"main.bicep defaults workload to {bicep_default.group(1)!r} but "
            f"main.parameters.json yields {shell!r}",
        )

    def test_every_shell_copy_matches_the_bicep(self) -> None:
        expected = self._expected_rg_expression()
        found = []
        for step in _steps():
            for line in str(step.get("run") or "").splitlines():
                match = re.match(r'\s*rg="([^"]+)"\s*$', line)
                if match:
                    found.append((step.get("name"), match.group(1)))
        self.assertGreaterEqual(
            len(found),
            2,
            "expected at least the custom-domain preflight and the image build "
            f"step to derive an rg= name; found {found}",
        )
        for name, derived in found:
            self.assertEqual(
                derived,
                expected,
                f"{name!r} derives {derived!r} but infra/main.bicep + "
                f"infra/main.parameters.json say {expected!r}. Every `az` lookup "
                "in that step would target a resource group that does not exist, "
                "and a missing app reads as 'nothing bound' -- so it fails "
                "silently rather than loudly.",
            )


if __name__ == "__main__":
    unittest.main()
