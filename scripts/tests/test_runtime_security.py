"""Paired controls for the build-only chiseled OpenSSL patch and final-image proof."""

from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import yaml

from scripts.tests._loader import load_script

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "proxy" / "runtime-security.py"
MANIFEST = ROOT / "proxy" / "runtime-security.json"
patcher = load_script("runtime_security_patch", SCRIPT, register=True)
OLD = "3.0.13-0ubuntu3.15"
NEW = "3.0.13-0ubuntu3.16"


def control(package: str, architecture: str, version: str) -> str:
    return (
        f"Package: {package}\n"
        + ("Source: openssl\n" if package == "libssl3t64" else "")
        + f"Version: {version}\nArchitecture: {architecture}\nDescription: fixture\n"
    )


def elf(architecture: str, label: str) -> bytes:
    _, kind, machine = patcher.ARCHITECTURES[architecture]
    result = bytearray(20)
    result[:4] = b"\x7fELF"
    result[4:6] = bytes([kind, 1])
    result[18:20] = machine.to_bytes(2, "little")
    return bytes(result) + label.encode()


def archive(
    files: dict[str, bytes], *, links: dict[str, str] | None = None,
    modes: dict[str, int] | None = None, owners: dict[str, int] | None = None,
) -> bytes:
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w") as tar:
        for filename, payload in files.items():
            member = tarfile.TarInfo(filename)
            member.size = len(payload)
            member.mode = (modes or {}).get(filename, 0o644)
            member.uid = (owners or {}).get(filename, 0)
            member.gid = 0
            tar.addfile(member, io.BytesIO(payload))
        for filename, target in (links or {}).items():
            member = tarfile.TarInfo(filename)
            member.type = tarfile.SYMTYPE
            member.linkname = target
            member.mode = 0o777
            tar.addfile(member)
    return result.getvalue()


class Fixture:
    """Only the dpkg transport and one filesystem symlink are synthetic."""

    def __init__(self, root: Path, architecture: str = "amd64") -> None:
        self.architecture = architecture
        self.root = root / "base"
        self.packages = root / "packages"
        self.output = root / "overlay"
        self.root.mkdir()
        self.packages.mkdir()
        self.payloads = {
            filename: b"fixture license" if filename == patcher.COPYRIGHT_PATH else elf(architecture, filename)
            for filename in patcher.file_paths(architecture)
        }
        self.old_status = (
            control("base-files", architecture, "24.04").rstrip("\n") + "\n\n"
            + "\n\n".join(control(name, architecture, OLD).rstrip("\n") for name in sorted(patcher.PACKAGES))
            + "\n"
        ).encode()
        self.base_write(patcher.STATUS_PATH, self.old_status)
        for filename in self.payloads:
            self.base_write(
                filename, b"old license" if filename == patcher.COPYRIGHT_PATH else elf(architecture, "old"),
            )
        self.link = self.root / patcher.COPYRIGHT_LINK
        self.link.parent.mkdir(parents=True, exist_ok=True)
        self.link.write_bytes(b"synthetic symlink")
        self.link_target = patcher.COPYRIGHT_TARGET
        self.extra_symlinks: set[Path] = set()
        self.decoded: dict[tuple[str, str], bytes] = {}
        pins = {}
        for name in sorted(patcher.PACKAGES):
            package = self.packages / f"{name}_{NEW}_{architecture}.deb"
            package.write_bytes(f"verified fixture package {name}".encode())
            pins[name] = patcher.PackagePin(patcher.digest(package.read_bytes()), package.stat().st_size)
            self.decoded[("--field", str(package))] = control(name, architecture, NEW).encode()
            files = self.payloads if name == "libssl3t64" else {"usr/bin/openssl": b"excluded CLI"}
            links = {patcher.COPYRIGHT_LINK: patcher.COPYRIGHT_TARGET} if name == "openssl" else {}
            self.decoded[("--fsys-tarfile", str(package))] = archive(files, links=links)
        self.manifest = patcher.Manifest(
            OLD, NEW, {architecture: patcher.Architecture(
                pins, {name: patcher.digest(value) for name, value in self.payloads.items()},
            )},
        )

    def base_write(self, filename: str, raw: bytes) -> None:
        path = self.root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    def context(self) -> ExitStack:
        stack = ExitStack()
        original_link, original_resolve, original_readlink = Path.is_symlink, Path.resolve, Path.readlink

        def is_symlink(path: Path) -> bool:
            return path == self.link or path in self.extra_symlinks or original_link(path)

        def resolve(path: Path, *args, **kwargs) -> Path:
            if path == self.link:
                return original_resolve(path.parent / self.link_target, *args, **kwargs)
            return original_resolve(path, *args, **kwargs)

        def readlink(path: Path) -> Path:
            return Path(self.link_target) if path == self.link else original_readlink(path)

        def decode(arguments: list[str], limit: int) -> bytes:
            raw = self.decoded[tuple(arguments)]
            if len(raw) > limit:
                raise patcher.PatchError("fixture decoder exceeds its bound")
            return raw

        stack.enter_context(mock.patch.object(Path, "is_symlink", is_symlink))
        stack.enter_context(mock.patch.object(Path, "resolve", resolve))
        stack.enter_context(mock.patch.object(Path, "readlink", readlink))
        stack.enter_context(mock.patch.object(patcher, "dpkg_output", side_effect=decode))
        return stack

    def prepare(self) -> str:
        with self.context():
            return patcher.prepare(self.root, self.packages, self.output, self.manifest)

    def final_files(self) -> dict[str, bytes]:
        return {
            **self.payloads,
            patcher.STATUS_PATH: (
                control("base-files", self.architecture, "24.04").rstrip("\n") + "\n\n"
                + "\n\n".join(
                    control(name, self.architecture, NEW).rstrip("\n") for name in sorted(patcher.PACKAGES)
                ) + "\n"
            ).encode(),
        }

    def verify(self, files: dict[str, bytes] | None = None, **kwargs) -> str:
        return patcher.verify(io.BytesIO(archive(
            self.final_files() if files is None else files,
            links={patcher.COPYRIGHT_LINK: patcher.COPYRIGHT_TARGET}, **kwargs,
        )), self.manifest)


class PreparationTests(unittest.TestCase):
    def test_all_architectures_replace_the_actual_subset_and_preserve_other_metadata(self) -> None:
        for architecture in patcher.ARCHITECTURES:
            with self.subTest(architecture=architecture), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(Path(directory), architecture)
                self.assertEqual(fixture.prepare(), architecture)
                written = {path.relative_to(fixture.output).as_posix() for path in fixture.output.rglob("*") if path.is_file()}
                self.assertEqual(written, {*fixture.payloads, patcher.STATUS_PATH})
                for filename, raw in fixture.payloads.items():
                    self.assertEqual((fixture.output / filename).read_bytes(), raw)
                status = (fixture.output / patcher.STATUS_PATH).read_bytes()
                self.assertIn(control("base-files", architecture, "24.04").rstrip("\n").encode(), status)
                self.assertEqual(status.count(f"Version: {NEW}\n".encode()), 2)
                self.assertEqual((fixture.root / patcher.STATUS_PATH).read_bytes(), fixture.old_status)
                self.assertFalse((fixture.output / "usr/bin/openssl").exists())
                self.assertEqual(fixture.verify(), architecture)

    def test_invalid_package_inputs_never_create_a_partial_overlay(self) -> None:
        for fault in ("checksum", "size", "version", "architecture", "missing payload", "changed payload", "extra package"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(Path(directory))
                package = fixture.packages / f"libssl3t64_{NEW}_amd64.deb"
                original = package.read_bytes()
                if fault == "checksum":
                    package.write_bytes(original.replace(b"verified", b"tampered"))
                elif fault == "size":
                    package.write_bytes(original + b"x")
                elif fault in ("version", "architecture"):
                    fixture.decoded[("--field", str(package))] = control(
                        "libssl3t64", "arm64" if fault == "architecture" else "amd64",
                        OLD if fault == "version" else NEW,
                    ).encode()
                elif fault in ("missing payload", "changed payload"):
                    values = dict(fixture.payloads)
                    selected = next(name for name in values if name.endswith("libssl.so.3"))
                    if fault == "missing payload":
                        del values[selected]
                    else:
                        values[selected] += b"tampered"
                    fixture.decoded[("--fsys-tarfile", str(package))] = archive(values)
                else:
                    (fixture.packages / "unexpected.deb").write_bytes(b"x")
                with self.assertRaises(patcher.PatchError):
                    fixture.prepare()
                self.assertFalse(fixture.output.exists())
            with tempfile.TemporaryDirectory() as directory:
                self.assertEqual(Fixture(Path(directory)).prepare(), "amd64")

    def test_base_metadata_missing_files_symlinks_and_preexisting_output_refuse(self) -> None:
        for fault in ("old version", "duplicate package", "missing file", "path symlink", "copyright link", "existing output"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(Path(directory))
                selected = next(name for name in fixture.payloads if name.endswith("libssl.so.3"))
                if fault == "old version":
                    fixture.base_write(patcher.STATUS_PATH, fixture.old_status.replace(OLD.encode(), b"unknown"))
                elif fault == "duplicate package":
                    fixture.base_write(patcher.STATUS_PATH, fixture.old_status + b"\n" + control("openssl", "amd64", OLD).encode())
                elif fault == "missing file":
                    (fixture.root / selected).unlink()
                elif fault == "path symlink":
                    fixture.extra_symlinks.add(fixture.root / selected)
                elif fault == "copyright link":
                    fixture.link_target = "../../../outside"
                else:
                    fixture.output.mkdir()
                with self.assertRaises(patcher.PatchError):
                    fixture.prepare()
                self.assertFalse(any(fixture.output.rglob("*")) if fixture.output.exists() else False)
            with tempfile.TemporaryDirectory() as directory:
                self.assertEqual(Fixture(Path(directory)).prepare(), "amd64")

    def test_decoding_never_runs_before_archive_integrity_is_proven(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            name = fixture.packages / f"libssl3t64_{NEW}_amd64.deb"
            original = name.read_bytes()
            with fixture.context(), mock.patch.object(patcher, "dpkg_output", wraps=patcher.dpkg_output) as decode:
                name.write_bytes(original.replace(b"verified", b"tampered"))
                with self.assertRaises(patcher.PatchError):
                    patcher.load_package(name, fixture.manifest.architectures["amd64"].packages["libssl3t64"], "libssl3t64", "amd64", fixture.manifest)
                decode.assert_not_called()
                name.write_bytes(original)
                patcher.load_package(name, fixture.manifest.architectures["amd64"].packages["libssl3t64"], "libssl3t64", "amd64", fixture.manifest)
                self.assertEqual(decode.call_count, 2)


class FinalImageTests(unittest.TestCase):
    def test_metadata_only_patch_and_partial_binary_patches_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            for name in sorted(fixture.payloads):
                if name == patcher.COPYRIGHT_PATH:
                    continue
                for metadata in (OLD, NEW):
                    with self.subTest(file=name, metadata=metadata):
                        files = fixture.final_files()
                        files[name] = elf("amd64", "old")
                        files[patcher.STATUS_PATH] = files[patcher.STATUS_PATH].replace(NEW.encode(), metadata.encode())
                        with self.assertRaises(patcher.PatchError):
                            fixture.verify(files)
                        self.assertEqual(fixture.verify(), "amd64")

    def test_file_permissions_ownership_missing_evidence_and_extra_runtime_tools_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            selected = next(name for name in fixture.payloads if name.endswith("libssl.so.3"))
            for kwargs in ({"modes": {selected: 0o666}}, {"owners": {selected: 1654}}):
                with self.assertRaises(patcher.PatchError):
                    fixture.verify(**kwargs)
                self.assertEqual(fixture.verify(), "amd64")
            for missing in (selected, patcher.STATUS_PATH):
                files = fixture.final_files()
                del files[missing]
                with self.assertRaises(patcher.PatchError):
                    fixture.verify(files)
                self.assertEqual(fixture.verify(), "amd64")
            for tool in ("bin/sh", "usr/bin/apt-get", "usr/bin/python3.12", "usr/lib/python3.12/os.py", "usr/bin/openssl"):
                with self.subTest(tool=tool), self.assertRaises(patcher.PatchError):
                    fixture.verify({**fixture.final_files(), tool: b"unexpected tool"})
                self.assertEqual(fixture.verify(), "amd64")

    def test_symlink_libraries_unsafe_paths_and_duplicate_selected_members_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            selected = next(name for name in fixture.payloads if name.endswith("libssl.so.3"))
            files = fixture.final_files()
            del files[selected]
            for raw in (
                archive(files, links={selected: "/outside", patcher.COPYRIGHT_LINK: patcher.COPYRIGHT_TARGET}),
                archive({**fixture.final_files(), "../outside": b"x"}, links={patcher.COPYRIGHT_LINK: patcher.COPYRIGHT_TARGET}),
            ):
                with self.assertRaises(patcher.PatchError):
                    patcher.verify(io.BytesIO(raw), fixture.manifest)
                self.assertEqual(fixture.verify(), "amd64")
            raw = archive(fixture.final_files(), links={patcher.COPYRIGHT_LINK: patcher.COPYRIGHT_TARGET})
            duplicate = io.BytesIO()
            with tarfile.open(fileobj=duplicate, mode="w") as tar:
                for _ in range(2):
                    member = tarfile.TarInfo(patcher.STATUS_PATH)
                    member.mode = 0o644
                    member.size = len(fixture.final_files()[patcher.STATUS_PATH])
                    tar.addfile(member, io.BytesIO(fixture.final_files()[patcher.STATUS_PATH]))
            with self.assertRaises(patcher.PatchError):
                patcher.verify(io.BytesIO(duplicate.getvalue()), fixture.manifest)
            self.assertEqual(patcher.verify(io.BytesIO(raw), fixture.manifest), "amd64")


class ManifestAndWiringTests(unittest.TestCase):
    def test_committed_manifest_is_complete_and_malformed_variants_refuse(self) -> None:
        manifest = patcher.Manifest.load(MANIFEST)
        self.assertEqual(set(manifest.architectures), set(patcher.ARCHITECTURES))
        source = json.loads(MANIFEST.read_text())
        variants = []
        for field in ("armhf", "arm64"):
            broken = copy.deepcopy(source)
            del broken["architectures"][field]
            variants.append(broken)
        for field, value in (("schemaVersion", True), ("version", OLD), ("source", "https://example.test")):
            variants.append({**source, field: value})
        broken = copy.deepcopy(source)
        broken["architectures"]["amd64"]["files"]["../../outside"] = "a" * 64
        variants.append(broken)
        for variant in variants:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "manifest.json"
                path.write_text(json.dumps(variant))
                with self.assertRaises(patcher.PatchError):
                    patcher.Manifest.load(path)
                self.assertEqual(patcher.Manifest.load(MANIFEST).version, NEW)

    def test_both_images_share_the_same_build_only_patch_and_final_verifier(self) -> None:
        bodies = [(ROOT / "proxy" / path).read_text() for path in ("Dockerfile", "CompanionApp.Dockerfile")]
        fragments = []
        for body in bodies:
            start = body.index("FROM sdk AS runtime-security")
            end = body.index("FROM sdk AS build-env")
            fragments.append(body[start:end])
            final = body[body.rindex("FROM "):]
            self.assertIn("FROM patched-runtime AS runtime", final)
            self.assertIn(
                "FROM runtime-base AS patched-runtime\nCOPY --from=runtime-security /runtime-security/overlay/ /",
                body,
            )
            self.assertNotIn("RUN ", final)
            self.assertNotIn("runtime-security.py", final)
            self.assertNotIn("apt-get", final)
        self.assertEqual(fragments[0], fragments[1])
        self.assertIn('apt-get download "libssl3t64=${version}" "openssl=${version}"', fragments[0])
        self.assertIn("python3=3.12.3-0ubuntu2.1", fragments[0])
        self.assertNotIn("cd ", fragments[0])
        self.assertNotIn("--allow-unauthenticated", fragments[0])
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "docker-build.yml").read_text())
        steps = workflow["jobs"]["api"]["steps"]
        for image in ("proxy", "CompanionApp"):
            name = f"Verify final {image} OpenSSL runtime bytes"
            step = next(item for item in steps if item.get("name") == name)
            self.assertIn("docker export", step["run"])
            self.assertIn("proxy/runtime-security.py verify", step["run"])
            self.assertNotIn("continue-on-error", step)
            self.assertLess(
                steps.index(step),
                next(index for index, item in enumerate(steps) if item.get("name") == f"Scan final {image} image for HIGH/CRITICAL vulnerabilities"),
            )
        quality = (ROOT / ".github" / "workflows" / "quality.yml").read_text()
        self.assertIn("python3 -m unittest scripts.tests.test_runtime_security", quality)
        ignored = (ROOT / "proxy" / ".trivyignore").read_text()
        self.assertNotRegex(ignored, r"(?m)^CVE-2026-84782\s*$")

    def test_cli_errors_are_explicit(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "verify", "--archive", str(ROOT / "absent-runtime.tar")],
            capture_output=True, text=True, check=False, timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Runtime security patch refused", result.stderr)
        self.assertNotIn("Verified chiseled", result.stdout)


if __name__ == "__main__":
    unittest.main()
