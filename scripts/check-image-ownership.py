#!/usr/bin/env python3
"""Check owners and modes in an exported container filesystem.

Chiseled images have no shell, so ownership cannot be probed with ``docker run``.
Instead, ``docker export`` of a created container gives the final numeric owner
and mode of every path after all layers are applied, and this script checks
that tar::

    docker export "$(docker create IMAGE)" | python3 scripts/check-image-ownership.py \
        --root-owned app --owned var/lib/companion/keys=1654

``--root-owned DIR`` requires DIR and everything beneath it to be owned by uid 0
and never group- or other-writable, so a non-root runtime user cannot replace
the application. ``--owned DIR=UID`` requires DIR to be a directory owned by UID
that is not world-writable. Exit status 0 means every check held, 1 means a
violation or a missing path, and 2 means unusable input.
"""

from __future__ import annotations

import argparse
import sys
import tarfile
from collections.abc import Sequence
from typing import IO

GROUP_OR_OTHER_WRITE = 0o022
OTHER_WRITE = 0o002


def _normalize(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name.strip("/")


def _parse_owned(values: Sequence[str]) -> dict[str, int]:
    owned: dict[str, int] = {}
    for value in values:
        path, separator, uid = value.rpartition("=")
        if not separator or not path or not uid.isdigit():
            raise ValueError(f"--owned expects DIR=UID, got {value!r}")
        owned[_normalize(path)] = int(uid)
    return owned


def check(stream: IO[bytes], root_owned: Sequence[str], owned: dict[str, int]) -> list[str]:
    roots = [_normalize(root) for root in root_owned]
    errors: list[str] = []
    seen: set[str] = set()
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for member in archive:
            name = _normalize(member.name)
            for root in roots:
                if name != root and not name.startswith(root + "/"):
                    continue
                if name == root:
                    seen.add(root)
                    if not member.isdir():
                        errors.append(f"/{name}: expected a directory")
                if member.uid != 0:
                    errors.append(f"/{name}: owned by uid {member.uid}, expected root")
                # A symlink's own mode is always 0777 and grants nothing.
                if not member.issym() and member.mode & GROUP_OR_OTHER_WRITE:
                    errors.append(f"/{name}: mode {member.mode:04o} is group- or other-writable")
            if name in owned:
                seen.add(name)
                uid = owned[name]
                if not member.isdir():
                    errors.append(f"/{name}: expected a directory")
                if member.uid != uid:
                    errors.append(f"/{name}: owned by uid {member.uid}, expected {uid}")
                if member.mode & OTHER_WRITE:
                    errors.append(f"/{name}: mode {member.mode:04o} is world-writable")
    for path in [*roots, *owned]:
        if path not in seen:
            errors.append(f"/{path}: not present in the exported filesystem")
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", nargs="?", default="-", help="exported tar (default: stdin)")
    parser.add_argument("--root-owned", action="append", default=[], metavar="DIR")
    parser.add_argument("--owned", action="append", default=[], metavar="DIR=UID")
    args = parser.parse_args(argv)
    if not args.root_owned and not args.owned:
        parser.error("nothing to check")
    try:
        owned = _parse_owned(args.owned)
        if args.archive == "-":
            errors = check(sys.stdin.buffer, args.root_owned, owned)
        else:
            with open(args.archive, "rb") as stream:
                errors = check(stream, args.root_owned, owned)
    except (OSError, ValueError, tarfile.TarError) as exc:
        print(f"::error::cannot check the exported filesystem: {exc}", file=sys.stderr)
        return 2
    for error in errors:
        print(f"::error::{error}", file=sys.stderr)
    if errors:
        return 1
    print("Exported filesystem ownership checks passed: "
          + ", ".join([*(f"/{_normalize(root)} root-owned" for root in args.root_owned),
                       *(f"/{path} uid {uid}" for path, uid in owned.items())]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
