#!/usr/bin/env python3
"""Patch only the existing chiseled OpenSSL slice; verify final exported bytes."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

STATUS_PATH = "var/lib/dpkg/status"
COPYRIGHT_PATH = "usr/share/doc/libssl3t64/copyright"
COPYRIGHT_LINK = "usr/share/doc/openssl/copyright"
COPYRIGHT_TARGET = "../libssl3t64/copyright"
PACKAGES = frozenset({"libssl3t64", "openssl"})
ARCHITECTURES = {
    "amd64": ("x86_64-linux-gnu", 2, 62),
    "arm64": ("aarch64-linux-gnu", 2, 183),
    "armhf": ("arm-linux-gnueabihf", 1, 40),
}
MAX_STATUS_BYTES = 128 * 1024
MAX_MANIFEST_BYTES = 16 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_PACKAGE_BYTES = 4 * 1024 * 1024
MAX_PACKAGE_TAR_BYTES = 16 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
VERSION = re.compile(r"3\.0\.13-0ubuntu3\.(\d+)\Z")


class PatchError(ValueError):
    """A malformed or mismatched input cannot produce a patch or verification."""


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_file(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise PatchError("Input file exceeds its byte bound.")
    return raw


def object_fields(value: object, expected: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise PatchError("Unexpected manifest fields.")
    return {key: value[key] for key in expected}


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PatchError("Duplicate manifest field.")
        result[key] = value
    return result


def file_paths(architecture: str) -> set[str]:
    triplet = ARCHITECTURES[architecture][0]
    prefix = f"usr/lib/{triplet}"
    return {
        f"{prefix}/libcrypto.so.3", f"{prefix}/libssl.so.3",
        f"{prefix}/ossl-modules/legacy.so", COPYRIGHT_PATH,
    }


@dataclass(frozen=True)
class PackagePin:
    sha256: str
    size: int


@dataclass(frozen=True)
class Architecture:
    packages: dict[str, PackagePin]
    files: dict[str, str]


@dataclass(frozen=True)
class Manifest:
    source_version: str
    version: str
    architectures: dict[str, Architecture]

    @classmethod
    def load(cls, path: Path) -> Manifest:
        raw = read_file(path, MAX_MANIFEST_BYTES)
        data = object_fields(json.loads(raw, object_pairs_hook=unique_object), {
            "schemaVersion", "source", "sourceVersion", "version", "architectures",
        })
        if type(data["schemaVersion"]) is not int or data["schemaVersion"] != 1:
            raise PatchError("Unsupported manifest version.")
        if data["source"] != "https://ubuntu.com/security/notices/USN-8847-1":
            raise PatchError("Unreviewed package authority.")
        before, after = data["sourceVersion"], data["version"]
        if not isinstance(before, str) or not isinstance(after, str):
            raise PatchError("Invalid package version.")
        old, new = VERSION.fullmatch(before), VERSION.fullmatch(after)
        if old is None or new is None or int(new[1]) <= int(old[1]):
            raise PatchError("The patch must advance the reviewed OpenSSL series.")
        rows = object_fields(data["architectures"], set(ARCHITECTURES))
        architectures: dict[str, Architecture] = {}
        for name, value in rows.items():
            row = object_fields(value, {"packages", "files"})
            packages = object_fields(row["packages"], set(PACKAGES))
            pins: dict[str, PackagePin] = {}
            for package, pin in packages.items():
                fields = object_fields(pin, {"sha256", "size"})
                checksum, size = fields["sha256"], fields["size"]
                if (
                    not isinstance(checksum, str) or not SHA256.fullmatch(checksum)
                    or type(size) is not int or not 0 < size <= MAX_PACKAGE_BYTES
                ):
                    raise PatchError("Invalid package integrity metadata.")
                pins[package] = PackagePin(checksum, size)
            files = object_fields(row["files"], file_paths(name))
            checksums: dict[str, str] = {}
            for filename, checksum in files.items():
                if not isinstance(checksum, str) or not SHA256.fullmatch(checksum):
                    raise PatchError("Invalid selected-file integrity metadata.")
                checksums[filename] = checksum
            architectures[name] = Architecture(pins, checksums)
        return cls(before, after, architectures)


def paragraph_fields(value: str) -> dict[str, str]:
    if "\r" in value or not value or len(value.encode()) > MAX_STATUS_BYTES:
        raise PatchError("Invalid package metadata.")
    result: dict[str, str] = {}
    previous: str | None = None
    for line in value.splitlines():
        if line.startswith((" ", "\t")) and previous is not None:
            result[previous] += "\n" + line
            continue
        key, separator, content = line.partition(": ")
        if not separator or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", key) or key in result:
            raise PatchError("Malformed or duplicate package field.")
        result[key] = content
        previous = key
    if not all(result.get(field) for field in ("Package", "Version", "Architecture")):
        raise PatchError("Incomplete package identity.")
    return result


def status_records(raw: bytes) -> list[tuple[str, dict[str, str]]]:
    if not raw or len(raw) > MAX_STATUS_BYTES:
        raise PatchError("Invalid status-file size.")
    records: list[tuple[str, dict[str, str]]] = []
    seen: set[str] = set()
    for paragraph in raw.decode("utf-8").strip("\n").split("\n\n"):
        fields = paragraph_fields(paragraph)
        name = fields["Package"]
        if name in seen:
            raise PatchError("Duplicate installed package.")
        seen.add(name)
        records.append((paragraph, fields))
    if not PACKAGES.issubset(seen):
        raise PatchError("The base is missing the reviewed OpenSSL slice.")
    return records


def selected_architecture(
    records: list[tuple[str, dict[str, str]]], manifest: Manifest, version: str,
) -> str:
    selected = [fields for _, fields in records if fields["Package"] in PACKAGES]
    architectures = {fields["Architecture"] for fields in selected}
    if len(architectures) != 1 or any(fields["Version"] != version for fields in selected):
        raise PatchError("The installed OpenSSL identity does not match this patch.")
    architecture = architectures.pop()
    if architecture not in manifest.architectures:
        raise PatchError("Unsupported runtime architecture.")
    return architecture


def archive_name(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    name = name.rstrip("/")
    if name in ("", "."):
        return ""
    if (
        name.startswith("/") or "\\" in name or len(name) > 256
        or any(part in ("", ".", "..") for part in name.split("/"))
    ):
        raise PatchError("Unsafe archive path.")
    return name


def read_member(archive: tarfile.TarFile, member: tarfile.TarInfo, limit: int) -> bytes:
    if (
        not member.isfile() or not 0 < member.size <= limit
        or member.uid != 0 or member.gid != 0 or member.mode != 0o644
    ):
        raise PatchError("Selected runtime files must be bounded, root-owned regular files.")
    stream = archive.extractfile(member)
    if stream is None:
        raise PatchError("Selected file has no bytes.")
    with stream:
        raw = stream.read(limit + 1)
    if len(raw) != member.size:
        raise PatchError("Selected file is incomplete.")
    return raw


def validate_payload(filename: str, raw: bytes, architecture: str, checksum: str) -> None:
    if digest(raw) != checksum:
        raise PatchError("Selected-file checksum mismatch.")
    if filename == COPYRIGHT_PATH:
        return
    _, elf_class, machine = ARCHITECTURES[architecture]
    if (
        len(raw) < 20 or raw[:4] != b"\x7fELF" or raw[4] != elf_class or raw[5] != 1
        or int.from_bytes(raw[18:20], "little") != machine
    ):
        raise PatchError("Selected library has the wrong ELF architecture.")


def dpkg_output(arguments: list[str], limit: int) -> bytes:
    result = subprocess.run(
        ["dpkg-deb", *arguments], capture_output=True, check=True, timeout=30,
    )
    if not result.stdout or len(result.stdout) > limit:
        raise PatchError("Package decoder output is out of bounds.")
    return result.stdout


def load_package(
    path: Path, pin: PackagePin, package: str, architecture: str, manifest: Manifest,
) -> tuple[str, dict[str, bytes]]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != pin.size:
        raise PatchError("Package file is missing or has the wrong size.")
    raw = read_file(path, pin.size)
    if digest(raw) != pin.sha256:
        raise PatchError("Package checksum mismatch.")
    control = dpkg_output(["--field", str(path)], MAX_STATUS_BYTES).decode("utf-8").strip("\n")
    fields = paragraph_fields(control)
    if (
        fields["Package"] != package or fields["Version"] != manifest.version
        or fields["Architecture"] != architecture
        or (package == "libssl3t64" and fields.get("Source") != "openssl")
    ):
        raise PatchError("Package identity differs from the reviewed artifact.")
    wanted = manifest.architectures[architecture].files if package == "libssl3t64" else {}
    found: dict[str, bytes] = {}
    seen: set[str] = set()
    copyright_link = False
    data = dpkg_output(["--fsys-tarfile", str(path)], MAX_PACKAGE_TAR_BYTES)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive:
            name = archive_name(member.name)
            if name in seen:
                raise PatchError("Duplicate package archive path.")
            seen.add(name)
            if name in wanted:
                payload = read_member(archive, member, MAX_FILE_BYTES)
                validate_payload(name, payload, architecture, wanted[name])
                found[name] = payload
            elif name == COPYRIGHT_LINK and package == "openssl":
                if (
                    not member.issym() or member.linkname != COPYRIGHT_TARGET
                    or member.uid != 0 or member.gid != 0
                ):
                    raise PatchError("The existing copyright link must not change.")
                copyright_link = True
    if set(found) != set(wanted) or (package == "openssl" and not copyright_link):
        raise PatchError("Package does not contain the complete reviewed slice.")
    return control, found


def base_file(root: Path, filename: str) -> Path:
    candidate = root
    for component in PurePosixPath(filename).parts:
        candidate /= component
        if candidate.is_symlink():
            raise PatchError("A selected base path traverses a symlink.")
    if not candidate.is_file():
        raise PatchError("The base is missing a selected regular file.")
    return candidate


def prepare(root: Path, packages: Path, output: Path, manifest: Manifest) -> str:
    if root.is_symlink() or packages.is_symlink() or output.exists() or output.is_symlink():
        raise PatchError("Inputs must be real directories and output must be new.")
    root = root.resolve(strict=True)
    if output.resolve() == root or root in output.resolve().parents:
        raise PatchError("The overlay must not modify its input runtime.")
    raw_status = read_file(base_file(root, STATUS_PATH), MAX_STATUS_BYTES)
    records = status_records(raw_status)
    architecture = selected_architecture(records, manifest, manifest.source_version)
    link = root / COPYRIGHT_LINK
    if (
        not link.is_symlink() or link.readlink().as_posix() != COPYRIGHT_TARGET
        or link.resolve() != (root / COPYRIGHT_PATH).resolve()
    ):
        raise PatchError("Unexpected base copyright link.")
    pins = manifest.architectures[architecture]
    controls: dict[str, str] = {}
    payloads: dict[str, bytes] = {}
    expected = {f"{name}_{manifest.version}_{architecture}.deb" for name in PACKAGES}
    if {path.name for path in packages.iterdir()} != expected:
        raise PatchError("The download directory must contain exactly the reviewed packages.")
    for package in sorted(PACKAGES):
        control, files = load_package(
            packages / f"{package}_{manifest.version}_{architecture}.deb",
            pins.packages[package], package, architecture, manifest,
        )
        controls[package] = control
        payloads.update(files)
    for filename, payload in payloads.items():
        original = base_file(root, filename)
        if filename != COPYRIGHT_PATH and digest(read_file(original, MAX_FILE_BYTES)) == digest(payload):
            raise PatchError("The original library is already patched but its metadata disagrees.")
    replacement = "\n\n".join(
        controls.get(fields["Package"], paragraph) for paragraph, fields in records
    ).encode() + b"\n"
    selected_architecture(status_records(replacement), manifest, manifest.version)
    payloads[STATUS_PATH] = replacement
    # All integrity, metadata and subset checks finish before any output is created.
    output.mkdir(mode=0o755)
    for filename, payload in sorted(payloads.items()):
        path = output / filename
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        with path.open("xb") as stream:
            stream.write(payload)
        path.chmod(0o644)
    return architecture


def verify(stream: BinaryIO, manifest: Manifest) -> str:
    wanted = {STATUS_PATH, COPYRIGHT_LINK}
    wanted.update(path for row in manifest.architectures.values() for path in row.files)
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    copyright_link = False
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for member in archive:
            name = archive_name(member.name)
            if name in wanted:
                if name in seen:
                    raise PatchError("Duplicate selected path in the exported filesystem.")
                seen.add(name)
                if name == COPYRIGHT_LINK:
                    if (
                        not member.issym() or member.linkname != COPYRIGHT_TARGET
                        or member.uid != 0 or member.gid != 0
                    ):
                        raise PatchError("Exported copyright link differs from the chiseled slice.")
                    copyright_link = True
                else:
                    files[name] = read_member(
                        archive, member, MAX_STATUS_BYTES if name == STATUS_PATH else MAX_FILE_BYTES,
                    )
            if name in {
                "bin/sh", "bin/bash", "usr/bin/sh", "usr/bin/bash", "usr/bin/apt",
                "usr/bin/apt-get", "usr/bin/dpkg", "usr/bin/openssl",
            } or name.startswith(("usr/bin/python", "usr/lib/python")):
                raise PatchError("The final runtime contains unapproved build or shell tools.")
    if STATUS_PATH not in files or not copyright_link:
        raise PatchError("Exported package metadata or copyright evidence is missing.")
    architecture = selected_architecture(
        status_records(files[STATUS_PATH]), manifest, manifest.version,
    )
    expected = manifest.architectures[architecture].files
    if set(files) != {*expected, STATUS_PATH}:
        raise PatchError("Exported runtime slice is missing or mixes architectures.")
    for filename, checksum in expected.items():
        validate_payload(filename, files[filename], architecture, checksum)
    return architecture


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("runtime-security.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version")
    patch = commands.add_parser("prepare")
    patch.add_argument("--root", type=Path, required=True)
    patch.add_argument("--packages", type=Path, required=True)
    patch.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--archive", default="-")
    args = parser.parse_args(argv)
    try:
        manifest = Manifest.load(args.manifest)
        if args.command == "version":
            print(manifest.version)
            return 0
        if args.command == "prepare":
            architecture = prepare(args.root, args.packages, args.output, manifest)
        elif args.archive == "-":
            architecture = verify(sys.stdin.buffer, manifest)
        else:
            with Path(args.archive).open("rb") as stream:
                architecture = verify(stream, manifest)
    except (
        OSError, ValueError, UnicodeError, tarfile.TarError,
        subprocess.CalledProcessError, subprocess.TimeoutExpired,
    ) as exc:
        print(f"::error::Runtime security patch refused: {exc}", file=sys.stderr)
        return 1
    print(f"Verified chiseled OpenSSL slice: {architecture}, {manifest.version}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
