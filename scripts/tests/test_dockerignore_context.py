"""Regression tests proving app/web and app/api .dockerignore files
recursively exclude dotenv secrets from the Docker build context, at any
directory depth, while still preserving committed .env.example files.

Docker's .dockerignore matching is NOT recursive by default the way Git's
.gitignore is: an unanchored gitignore pattern such as `.env*` matches at
any depth, but the equivalent .dockerignore pattern only matches at the
root of the build context. A nested dotenv file (e.g. accidentally added
in a subdirectory) can therefore be invisible to `git status` (already
gitignored by the repo's recursive .gitignore patterns) yet still get
copied into a Docker image via `COPY . .` / `COPY src ./src` plus
`cache-to: mode=max`, unless the .dockerignore pattern is explicitly
anchored with a `**/` prefix.

This test builds a real, throwaway Docker image from each app's *actual,
committed* .dockerignore file plus synthetic root- and nested-depth dotenv
files, then inspects the file listing that landed inside the image. It
exercises the real Docker engine instead of reimplementing dockerignore
glob semantics in Python, so it can't silently drift from actual build
behavior. Skipped automatically when the `docker` CLI isn't available.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DOCKER_AVAILABLE = shutil.which("docker") is not None

# Synthetic secret (dotenv) and example files created at root and nested
# depths inside the probe build context. File contents are irrelevant;
# only presence/absence in the built image is asserted.
SECRET_FILES = [
    ".env",
    ".env.local",
    "nested/.env",
    "nested/.env.production.local",
    "nested/sub/.env.development",
]
EXAMPLE_FILES = [
    ".env.example",
    "nested/.env.example",
]
CONTROL_FILE = "normal.txt"

# Minimal single-stage Dockerfile: copies the probe context (subject to the
# real .dockerignore under test) and lists every file that made it in.
PROBE_DOCKERFILE = """\
FROM alpine:3.20
WORKDIR /probe
COPY . .
CMD ["find", ".", "-type", "f"]
"""


def _build_and_list_files(dockerignore_path: Path) -> set[str]:
    """Build a throwaway image using *dockerignore_path* plus the synthetic
    secret/example/control files, run it, and return the set of relative
    (POSIX-style, no leading "./") file paths that landed inside the image.
    """
    with tempfile.TemporaryDirectory(prefix="dockerignore-context-") as tmp:
        probe = Path(tmp)
        shutil.copyfile(dockerignore_path, probe / ".dockerignore")
        (probe / "Dockerfile").write_text(PROBE_DOCKERFILE, encoding="utf-8")
        (probe / CONTROL_FILE).write_text("control\n", encoding="utf-8")
        for rel in SECRET_FILES + EXAMPLE_FILES:
            path = probe / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("secret-or-example\n", encoding="utf-8")

        tag = f"dockerignore-context-probe:{uuid.uuid4().hex[:12]}"
        try:
            subprocess.run(
                ["docker", "build", "-t", tag, "."],
                cwd=probe,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
            result = subprocess.run(
                ["docker", "run", "--rm", tag],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        finally:
            subprocess.run(
                ["docker", "image", "rm", "-f", tag],
                capture_output=True,
                text=True,
                timeout=60,
            )

    files: set[str] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line in (".", "./"):
            continue
        files.add(line[2:] if line.startswith("./") else line)
    return files


@unittest.skipUnless(DOCKER_AVAILABLE, "docker CLI not available")
class DockerignoreContextTests(unittest.TestCase):
    def _assert_recursive_dotenv_handling(self, dockerignore_path: Path) -> None:
        files = _build_and_list_files(dockerignore_path)

        self.assertIn(
            CONTROL_FILE,
            files,
            "control file missing from the built image - COPY did not run "
            "as expected, so this test's other assertions would be moot",
        )
        for rel in EXAMPLE_FILES:
            self.assertIn(
                rel,
                files,
                f"{rel} should survive the !**/.env.example negation at "
                "its depth, but was excluded from the build context",
            )
        for rel in SECRET_FILES:
            self.assertNotIn(
                rel,
                files,
                f"{rel} leaked into the Docker build context - the "
                "dockerignore pattern is not recursively excluding dotenv "
                "secrets at every directory depth",
            )

    def test_web_dockerignore_excludes_nested_dotenv_secrets(self) -> None:
        self._assert_recursive_dotenv_handling(
            ROOT / "app" / "web" / ".dockerignore"
        )

    def test_api_dockerignore_excludes_nested_dotenv_secrets(self) -> None:
        self._assert_recursive_dotenv_handling(
            ROOT / "app" / "api" / ".dockerignore"
        )


if __name__ == "__main__":
    unittest.main()
