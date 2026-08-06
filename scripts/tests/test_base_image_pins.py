"""Base images CI builds must be pinned to an immutable digest (finding P1-7).

A tag is a mutable pointer. `docker-build.yml` resolves `node:22-alpine` when a
PR runs; `deploy.yml` resolves it again later. Nothing guarantees the two see
the same bytes, and nothing in the repository records which bytes were used --
so "CI is green" never proved anything about what production runs. This is not
theoretical: when these pins were taken, `python:3.12-slim` had moved the
previous day.

Pinning `tag@sha256:<digest>` fixes that for the two images CI actually builds.
The digest is the enforcement; the tag is kept because Dependabot's docker
ecosystem parses and rewrites the pair, and because a bare digest tells a human
reader nothing about the major version.

Three properties are worth a gate, and each maps to a way the pin quietly rots:

1. **Coverage.** A pin is only trustworthy where something resolves it. The
   required-to-pin set is therefore *derived from* `docker-build.yml`, not
   hand-listed -- adding a build job automatically makes that Dockerfile's pin
   mandatory, and a Dockerfile CI never builds must be recorded as a conscious
   exemption instead of silently passing.

2. **Consistency inside a file.** `app/web/Dockerfile` is multi-stage and names
   its base three times. A partial edit leaves two stages on one digest and one
   on another -- a legal, buildable Dockerfile that silently mixes two Node
   builds.

3. **The pinned major vs the CI toolchain.** AGENTS.md has always said the image
   major must track `app-ci.yml`'s `setup-node` / `setup-python` version, and
   the repo has drifted twice anyway (node 22 -> 26, python 3.12 -> 3.14), both
   caught only at audit. Prose is not a gate; this is.

Static and offline on purpose. Whether a digest *resolves* is proved by
`docker-build.yml` actually building both images on every PR -- a wrong digest
fails there, loudly, on the PR that introduces it.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

# A hard import, deliberately not guarded by `unittest.skipIf`: a gate that
# skips silently reports success while checking nothing. The workflow step
# installs it.
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKER_BUILD = ROOT / ".github/workflows/docker-build.yml"
APP_CI = ROOT / ".github/workflows/app-ci.yml"

# Vendored upstream trees. `proxy/SimpleL7Proxy/` is microsoft/SimpleL7Proxy at a
# pinned commit and carries its own upstream Dockerfile, which AI4IA does not
# build (the AI4IA image is `proxy/Dockerfile`, one level up) and must not edit.
# `.yamllint` excludes the same path for the same reason.
VENDORED_PREFIXES = ("proxy/SimpleL7Proxy/",)

# Tracked Dockerfiles that deliberately carry no digest pin, with the reason.
# Listing one here is a claim that CI cannot verify a pin for it; the test below
# checks that claim against docker-build.yml rather than trusting it.
UNPINNED_DOCKERFILES = {
    "proxy/Dockerfile": (
        "docker-build.yml deliberately keeps the vendored SimpleL7Proxy image out "
        "of scope to avoid touching the gateway build path, so a digest here would "
        "be unverified: a typo would first surface as a failed `azd deploy`. Its "
        "bases are mcr.microsoft.com/dotnet/{sdk,aspnet} tags, which stay mutable. "
        "Layer B still deploys the proxy by digest, so the RUNNING artifact is "
        "identified even though its base is not."
    ),
}

# `FROM [--flag=value ...] <ref> [AS <stage>]`
FROM_LINE = re.compile(
    r"^\s*FROM\s+(?:--\S+\s+)*(?P<ref>\S+)(?:\s+AS\s+(?P<stage>\S+))?\s*$",
    re.IGNORECASE,
)

# `<name>:<tag>@sha256:<64 lowercase hex>`. The tag is required, not optional:
# a bare `name@sha256:...` is still immutable but drops the version a reader and
# Dependabot both rely on.
PINNED_REF = re.compile(
    r"^(?P<name>[^@\s]+):(?P<tag>[^@:/]+)@sha256:(?P<digest>[0-9a-f]{64})$"
)

# Expected (image name, tag prefix) per CI toolchain version key.
CI_TOOLCHAIN = {
    "app/web/Dockerfile": ("node", "node-version"),
    "app/api/Dockerfile": ("python", "python-version"),
}


def _tracked_dockerfiles() -> list[str]:
    """Every Dockerfile this repo owns, vendored upstream trees excluded."""

    out = subprocess.run(
        ["git", "ls-files", "--", "*Dockerfile", "*Dockerfile.*"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(
        line.strip()
        for line in out.splitlines()
        if line.strip() and not line.strip().startswith(VENDORED_PREFIXES)
    )


def _parse_froms(path: str) -> list[str]:
    """External base-image references in ``path``, skipping intra-file stages."""

    stages: set[str] = set()
    refs: list[str] = []
    for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
        match = FROM_LINE.match(line)
        if not match:
            continue
        ref = match.group("ref")
        stage = match.group("stage")
        # `FROM builder AS runner` names an earlier stage, not a registry image.
        if ref.lower() not in stages:
            refs.append(ref)
        if stage:
            stages.add(stage.lower())
    return refs


def _dockerfiles_built_by_ci() -> set[str]:
    document = yaml.safe_load(DOCKER_BUILD.read_text(encoding="utf-8"))
    built: set[str] = set()
    for job in (document.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            uses = str(step.get("uses") or "")
            if uses.startswith("docker/build-push-action"):
                target = ((step.get("with") or {}).get("file")) or ""
                if target:
                    built.add(str(target))
            # A hand-rolled `docker buildx build --file X` counts too.
            for match in re.finditer(r"--file[=\s]+(\S+)", str(step.get("run") or "")):
                built.add(match.group(1))
    return built


def _setup_versions() -> dict[str, str]:
    """`node-version` / `python-version` as declared in app-ci.yml."""

    document = yaml.safe_load(APP_CI.read_text(encoding="utf-8"))
    versions: dict[str, str] = {}
    for job in (document.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            with_block = step.get("with") or {}
            for key in ("node-version", "python-version"):
                if key in with_block:
                    versions[key] = str(with_block[key])
    return versions


class BaseImagePinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dockerfiles = _tracked_dockerfiles()
        self.built = _dockerfiles_built_by_ci()

    def test_discovery_is_not_vacuous(self) -> None:
        """A regex that matches nothing would pass every assertion below."""

        self.assertIn("app/web/Dockerfile", self.dockerfiles)
        self.assertIn("app/api/Dockerfile", self.dockerfiles)
        self.assertIn("proxy/Dockerfile", self.dockerfiles)
        # web has three stages on the same base, api has one.
        self.assertEqual(len(_parse_froms("app/web/Dockerfile")), 3)
        self.assertEqual(len(_parse_froms("app/api/Dockerfile")), 1)
        # proxy's two FROMs are external; the `--from=build-env` COPY is not one.
        self.assertEqual(len(_parse_froms("proxy/Dockerfile")), 2)

    def test_the_vendored_exclusion_is_narrow(self) -> None:
        """The vendored filter must hide upstream's file and nothing of ours."""

        self.assertTrue(
            (ROOT / "proxy/SimpleL7Proxy/Dockerfile").is_file(),
            "the vendored upstream Dockerfile this filter exists for is gone; "
            "drop VENDORED_PREFIXES rather than leaving a blanket exclusion",
        )
        self.assertNotIn("proxy/SimpleL7Proxy/Dockerfile", self.dockerfiles)
        for path in self.dockerfiles:
            self.assertFalse(path.startswith(VENDORED_PREFIXES), path)

    def test_docker_build_workflow_builds_the_two_app_images(self) -> None:
        """The claim 'a wrong digest fails on the PR' depends on this."""

        self.assertEqual(self.built, {"app/web/Dockerfile", "app/api/Dockerfile"})

    def test_every_tracked_dockerfile_is_classified(self) -> None:
        classified = self.built | set(UNPINNED_DOCKERFILES)
        self.assertEqual(
            set(self.dockerfiles),
            classified,
            "a tracked Dockerfile is neither built by docker-build.yml nor "
            "recorded in UNPINNED_DOCKERFILES. Adding an image should be a "
            "deliberate choice between 'CI builds it, so pin it' and 'CI does "
            "not, and here is why that is acceptable'.",
        )

    def test_ci_built_images_are_pinned_by_digest(self) -> None:
        for path in sorted(self.built):
            refs = _parse_froms(path)
            self.assertTrue(refs, f"{path}: no FROM lines were parsed")
            for ref in refs:
                self.assertRegex(
                    ref,
                    PINNED_REF,
                    f"{path}: base image {ref!r} is not pinned as "
                    "`name:tag@sha256:<64 hex>`. A moving tag lets the image CI "
                    "tested and the image production runs diverge with no diff "
                    "to show for it.",
                )

    def test_one_digest_per_repository_tag_in_a_file(self) -> None:
        for path in sorted(self.built):
            seen: dict[str, str] = {}
            for ref in _parse_froms(path):
                match = PINNED_REF.match(ref)
                if not match:
                    continue
                key = f"{match.group('name')}:{match.group('tag')}"
                digest = match.group("digest")
                self.assertEqual(
                    seen.setdefault(key, digest),
                    digest,
                    f"{path}: {key} is pinned to two different digests. A "
                    "multi-stage build would then mix two different base images.",
                )

    def test_pinned_major_matches_the_ci_toolchain(self) -> None:
        """AGENTS.md's oldest base-image invariant, finally enforced."""

        versions = _setup_versions()
        self.assertEqual(
            set(versions),
            {"node-version", "python-version"},
            "app-ci.yml no longer declares both toolchain versions",
        )
        for path, (image, version_key) in CI_TOOLCHAIN.items():
            declared = versions[version_key]
            refs = _parse_froms(path)
            self.assertTrue(refs, f"{path}: no FROM lines were parsed")
            for ref in refs:
                match = PINNED_REF.match(ref)
                self.assertIsNotNone(match, f"{path}: {ref!r} is not a pinned ref")
                assert match is not None  # narrowing for the type checker
                self.assertEqual(
                    match.group("name"),
                    image,
                    f"{path}: expected a {image} base image, found {ref!r}",
                )
                tag = match.group("tag")
                self.assertTrue(
                    tag == declared or tag.startswith(f"{declared}-"),
                    f"{path}: base image tag {tag!r} does not track app-ci.yml's "
                    f"{version_key}: {declared!r}. Bumping one without the other "
                    "ships a runtime no CI job has exercised -- which is exactly "
                    "how node:26-alpine and python:3.14-slim both reached main.",
                )

    def test_exemptions_are_genuinely_unbuilt_and_still_exist(self) -> None:
        """Keeps the exemption list from outliving its own justification."""

        for path, reason in UNPINNED_DOCKERFILES.items():
            self.assertTrue((ROOT / path).is_file(), f"{path} no longer exists")
            self.assertNotIn(
                path,
                self.built,
                f"{path} is now built by docker-build.yml, so CI can verify a "
                "digest pin for it. Pin it and drop the exemption.",
            )
            self.assertGreater(len(reason), 80, f"{path}: record a real reason")


if __name__ == "__main__":
    unittest.main()
