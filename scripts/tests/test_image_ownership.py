"""Offline controls for scripts/check-image-ownership.py.

Synthetic tars stand in for ``docker export`` output: numeric owners and modes are
exactly what the exporter records. Every violation is paired with the passing
image it was derived from.
"""

from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.tests._loader import load_script

ROOT = Path(__file__).resolve().parents[2]
ownership = load_script("check_image_ownership", ROOT / "scripts" / "check-image-ownership.py")

APP_UID = 1654
ROOTS = ["app"]
OWNED = {"var/lib/companion/keys": APP_UID}


def entry(name: str, *, uid: int = 0, mode: int = 0o644, kind: bytes = tarfile.REGTYPE) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.uid = info.gid = uid
    info.mode = mode
    info.type = kind
    if kind == tarfile.SYMTYPE:
        info.linkname = "CompanionApp.dll"
    return info


def good_image() -> list[tarfile.TarInfo]:
    """The filesystem the fixed Dockerfile produces (paths as docker export writes them)."""
    return [
        entry(".dockerenv", mode=0o755),
        entry("app", mode=0o755, kind=tarfile.DIRTYPE),
        entry("app/CompanionApp.dll"),
        entry("app/wwwroot", mode=0o755, kind=tarfile.DIRTYPE),
        entry("app/wwwroot/_framework", mode=0o755, kind=tarfile.DIRTYPE),
        entry("app/wwwroot/_framework/blazor.web.js"),
        entry("app/current.dll", mode=0o777, kind=tarfile.SYMTYPE),
        entry("home/app", uid=APP_UID, mode=0o755, kind=tarfile.DIRTYPE),
        entry("var/lib/companion", mode=0o755, kind=tarfile.DIRTYPE),
        entry("var/lib/companion/keys", uid=APP_UID, mode=0o755, kind=tarfile.DIRTYPE),
    ]


def archive(members: list[tarfile.TarInfo]) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for member in members:
            tar.addfile(member, io.BytesIO(b"") if member.isreg() else None)
    buffer.seek(0)
    return buffer


def replace(name: str, **changes) -> list[tarfile.TarInfo]:
    members = good_image()
    for index, member in enumerate(members):
        if member.name == name:
            kind = changes.pop("kind", member.type)
            members[index] = entry(name, uid=changes.get("uid", member.uid),
                                   mode=changes.get("mode", member.mode), kind=kind)
    return members


class OwnershipCheckTests(unittest.TestCase):
    def test_the_fixed_image_passes(self) -> None:
        self.assertEqual(ownership.check(archive(good_image()), ROOTS, OWNED), [])

    def test_violations_are_reported_against_the_same_image(self) -> None:
        cases = {
            # The original defect: WORKDIR created /app as the chiseled base's app user.
            "app directory owned by the app user": (replace("app", uid=APP_UID), "/app: owned by uid 1654"),
            "application file owned by the app user": (
                replace("app/CompanionApp.dll", uid=APP_UID), "/app/CompanionApp.dll: owned by uid 1654"),
            "group-writable application file": (
                replace("app/CompanionApp.dll", mode=0o664), "/app/CompanionApp.dll: mode 0664"),
            "world-writable nested directory": (
                replace("app/wwwroot/_framework", mode=0o777), "/app/wwwroot/_framework: mode 0777"),
            "symlink owned by the app user": (
                replace("app/current.dll", uid=APP_UID), "/app/current.dll: owned by uid 1654"),
            "key ring owned by root": (
                replace("var/lib/companion/keys", uid=0), "/var/lib/companion/keys: owned by uid 0"),
            "world-writable key ring": (
                replace("var/lib/companion/keys", mode=0o777), "/var/lib/companion/keys: mode 0777"),
            "application root is a file": (
                replace("app", mode=0o755, kind=tarfile.REGTYPE), "/app: expected a directory"),
            "missing key ring": (
                [m for m in good_image() if m.name != "var/lib/companion/keys"],
                "/var/lib/companion/keys: not present"),
            "missing application root": (
                [m for m in good_image() if m.name != "app"], "/app: not present"),
        }
        for label, (members, expected) in cases.items():
            with self.subTest(case=label):
                errors = ownership.check(archive(members), ROOTS, OWNED)
                self.assertTrue(any(error.startswith(expected) for error in errors), errors)
        # Control: the unmutated image, and paths outside the checked roots, pass.
        self.assertEqual(ownership.check(archive(good_image()), ROOTS, OWNED), [])

    def test_prefix_siblings_are_not_treated_as_the_root(self) -> None:
        members = [*good_image(), entry("appdata", uid=APP_UID, mode=0o777, kind=tarfile.DIRTYPE)]
        self.assertEqual(ownership.check(archive(members), ROOTS, OWNED), [])

    def test_main_reads_a_tar_path_and_reports_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.tar"
            bad = Path(tmp) / "bad.tar"
            good.write_bytes(archive(good_image()).getvalue())
            bad.write_bytes(archive(replace("app", uid=APP_UID)).getvalue())
            args = ["--root-owned", "app", "--owned", "var/lib/companion/keys=1654"]
            self.assertEqual(ownership.main([str(good), *args]), 0)
            self.assertEqual(ownership.main([str(bad), *args]), 1)
            self.assertEqual(ownership.main([str(Path(tmp) / "absent.tar"), *args]), 2)
            self.assertEqual(ownership.main([str(good), "--root-owned", "app", "--owned", "keys"]), 2)


if __name__ == "__main__":
    unittest.main()
