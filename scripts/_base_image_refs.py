"""Shared, offline discovery of the repository's owned Dockerfile base pins."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

VENDORED_PREFIXES = ("proxy/SimpleL7Proxy/",)
FROM_LINE = re.compile(
    r"^\s*FROM\s+(?:--\S+\s+)*(?P<ref>\S+)(?:\s+AS\s+(?P<stage>\S+))?\s*$",
    re.IGNORECASE,
)
PINNED_REF = re.compile(
    r"^(?P<name>[^@\s]+):(?P<tag>[^@:/]+)@sha256:(?P<digest>[0-9a-f]{64})$"
)


class BaseSourceError(ValueError):
    """A fixed, content-free reason why base coverage cannot be established."""


def tracked_dockerfiles(root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "*Dockerfile", "*Dockerfile.*"],
            cwd=root, capture_output=True, check=True, timeout=10,
        )
        paths = sorted(
            name for name in result.stdout.decode("utf-8").split("\0")
            if name and not name.startswith(VENDORED_PREFIXES)
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise BaseSourceError("source_inventory_unavailable") from exc
    if not paths or len(paths) > 32:
        raise BaseSourceError("source_inventory_out_of_bounds")
    for name in paths:
        if (
            not name.isascii() or any(ord(char) < 32 for char in name)
            or not (root / name).resolve().is_relative_to(root.resolve())
        ):
            raise BaseSourceError("invalid_source_path")
    return paths


def external_references(path: Path) -> list[tuple[int, str]]:
    try:
        with path.open("rb") as source:
            body = source.read(128 * 1024 + 1)
        if len(body) > 128 * 1024:
            raise BaseSourceError("dockerfile_too_large")
        text = body.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise BaseSourceError("dockerfile_unavailable") from exc
    stages: set[str] = set()
    references: list[tuple[int, str]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not re.match(r"^\s*FROM\b", line, re.IGNORECASE):
            continue
        match = FROM_LINE.fullmatch(line)
        if not match:
            raise BaseSourceError("unsupported_from_statement")
        reference = match.group("ref")
        if reference.lower() not in stages:
            references.append((line_number, reference))
        if stage := match.group("stage"):
            stages.add(stage.lower())
    if not references:
        raise BaseSourceError("no_external_bases")
    return references
