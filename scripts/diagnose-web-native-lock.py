#!/usr/bin/env python3
"""Temporary PR477 evidence collection, never a repository-writing repair command."""
from __future__ import annotations

import argparse
import base64
import ctypes
import difflib
import gzip
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import zlib
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "ian-t-adams/AI4IA"
ORIGINAL = "da5a1c7c645b17babd17a78d8a1232145fb32ea0"
APPROVED_MAIN = "85fa75f117cfadab07500aa444ea056e940bb129"
ORIGINAL_HASHES = {
    "package.json": "b4e912890e0f5ec8bb8a03c1c75132d5c8340720bf3c3160d86dcc00ac983b37",
    "package-lock.json": "ebffc7076230bcc39320009b4d3ececdac8ba3a0d30c4d6448a772dfdf844264",
}
NODE_VERSION = "22.23.2"
# Read from npm/package.json in the retained Node distribution; also checked
# against nodejs.org's exact release row before package resolution or installation.
NPM_VERSION = "10.9.8"
VERSION = "16.3.5"
NATIVE = "@next/swc-win32-x64-msvc"
NATIVE_KEY = f"node_modules/{NATIVE}"
NODE_INDEX = "https://nodejs.org/dist/index.json"
REGISTRY = "https://registry.npmjs.org"
LOCK_LIMIT = 1024 * 1024
REPORT_LIMIT = 64 * 1024
SOURCE_LIMIT = 64 * 1024 * 1024
TARBALL_LIMIT = 256 * 1024 * 1024
UNPACKED_LIMIT = 512 * 1024 * 1024
OUTPUT_LIMIT = 4 * 1024 * 1024
TOTAL_SECONDS = 25 * 60
STAGES = (
    "source", "toolchain", "public_packages", "original_install", "original_guard",
    "original_native", "candidate_lock", "candidate_projection", "candidate_delta", "candidate_install",
    "candidate_guard", "candidate_native", "candidate_tests", "candidate_lint",
    "candidate_typecheck", "candidate_build", "immutability",
)
NPM_FLAGS = [
    "--ignore-scripts", "--no-audit", "--no-fund", "--include=optional",
    f"--registry={REGISTRY}", f"--@next:registry={REGISTRY}",
    "--fetch-retries=0", "--fetch-timeout=20000",
]
IDENTITY_ENV = (
    "GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_SERVER_URL", "GITHUB_EVENT_NAME",
    "GITHUB_REF", "GITHUB_EVENT_PATH", "GITHUB_SHA", "GITHUB_WORKFLOW_SHA",
    "GITHUB_WORKFLOW_REF", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "RUNNER_OS", "RUNNER_ARCH",
)
PUBLIC_OPERATIONS = ("node_release", "next_metadata", "next_tarball", "native_metadata", "native_tarball")
NPM_ERRORS = {
    "EUSAGE", "E404", "EINTEGRITY", "ERESOLVE", "EBADENGINE", "ENOENT", "ENOTCACHED",
    "ECONNRESET", "ECONNREFUSED", "ETIMEDOUT", "EAI_AGAIN", "EACCES", "EPERM", "ENOTFOUND",
    "ERR_SSL_SSL/TLS_ALERT_HANDSHAKE_FAILURE",
}


class DiagnosticError(ValueError):
    """A fixed content-free reason, never a registry body or raw command error."""

    def __init__(self, reason: str, http_status: int | None = None):
        super().__init__(reason)
        self.http_status = http_status


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise DiagnosticError(reason)


def encode(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def npm_json(value: dict) -> bytes:
    try:
        return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    except UnicodeError as exc:
        raise DiagnosticError("invalid_lock_utf8") from exc


def strict_json(data: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate_json_field")
            result[key] = value
        return result

    def constant(_value):
        raise DiagnosticError("non_json_constant")

    try:
        return json.loads(data, object_pairs_hook=unique, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise DiagnosticError("invalid_json") from exc


def read_bytes(path: Path, limit: int) -> bytes:
    require(path.is_file() and not path.is_symlink(), "regular_file_required")
    with path.open("rb") as stream:
        result = stream.read(limit + 1)
    require(0 < len(result) <= limit, "file_size_limit")
    return result


def object_json(data: bytes) -> dict:
    value = strict_json(data)
    require(isinstance(value, dict), "json_object_required")
    return value


def identity(env: dict[str, str]) -> dict:
    require(
        env.get("GITHUB_ACTIONS") == "true"
        and env.get("GITHUB_REPOSITORY") == REPOSITORY
        and env.get("GITHUB_SERVER_URL") == "https://github.com"
        and env.get("GITHUB_EVENT_NAME") == "pull_request"
        and env.get("GITHUB_REF") == "refs/pull/477/merge"
        and env.get("RUNNER_OS") == "Windows"
        and env.get("RUNNER_ARCH") == "X64",
        "only_pr477_windows_actions",
    )
    event = object_json(read_bytes(Path(env.get("GITHUB_EVENT_PATH", "")), LOCK_LIMIT))
    pr = event.get("pull_request", {})
    require(
        isinstance(pr, dict) and type(event.get("number")) is int and event["number"] == 477
        and type(pr.get("number")) is int and pr["number"] == 477
        and isinstance(pr.get("head"), dict)
        and isinstance(pr["head"].get("repo"), dict)
        and pr["head"]["repo"].get("full_name") == REPOSITORY,
        "unexpected_pull_request",
    )
    source = pr["head"].get("sha", "")
    require(isinstance(source, str) and bool(re.fullmatch("[a-f0-9]{40}", source)), "invalid_source_sha")
    for field in ("GITHUB_SHA", "GITHUB_WORKFLOW_SHA"):
        require(bool(re.fullmatch("[a-f0-9]{40}", env.get(field, ""))), "invalid_workflow_sha")
    require(
        env.get("GITHUB_WORKFLOW_REF")
        == f"{REPOSITORY}/.github/workflows/app-ci.yml@refs/pull/477/merge"
        and bool(re.fullmatch("[1-9][0-9]{0,19}", env.get("GITHUB_RUN_ID", "")))
        and bool(re.fullmatch("[1-9][0-9]{0,5}", env.get("GITHUB_RUN_ATTEMPT", ""))),
        "invalid_workflow_identity",
    )
    return {
        "repository": REPOSITORY, "pullRequest": 477, "sourceCommit": source,
        "workflowCommit": env["GITHUB_WORKFLOW_SHA"], "eventCommit": env["GITHUB_SHA"],
        "runId": env["GITHUB_RUN_ID"], "runAttempt": env["GITHUB_RUN_ATTEMPT"],
        "originalManifestCommit": ORIGINAL, "approvedMain": APPROVED_MAIN,
    }


def child_env(env: dict[str, str], home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    for name in ("user.npmrc", "global.npmrc"):
        (home / name).touch(exist_ok=False)
    allowed = {"PATH", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "WINDIR"}
    result = {key.upper(): value for key, value in env.items() if key.upper() in allowed}
    result.update({
        "HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(home),
        "LOCALAPPDATA": str(home), "TEMP": str(home), "TMP": str(home),
        "CI": "true", "NEXT_TELEMETRY_DISABLED": "1",
        "NPM_CONFIG_USERCONFIG": str(home / "user.npmrc"),
        "NPM_CONFIG_GLOBALCONFIG": str(home / "global.npmrc"),
        "NPM_CONFIG_CACHE": str(home / "cache"), "NPM_CONFIG_REGISTRY": REGISTRY,
        "NPM_CONFIG_IGNORE_SCRIPTS": "true", "NPM_CONFIG_STRICT_SSL": "true",
        "NPM_CONFIG_AUDIT": "false", "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false", "NPM_CONFIG_FETCH_RETRIES": "0",
        "NPM_CONFIG_FETCH_TIMEOUT": "20000", "NPM_CONFIG_MAXSOCKETS": "4",
    })
    return result


@dataclass
class CommandResult:
    status: str
    exit_code: int | None
    output: bytes = b""
    reason: str | None = None

    def summary(self) -> dict:
        codes = sorted({
            code.decode("ascii") for code in re.findall(
                rb"(?m)^npm error code ([A-Z0-9_/]{1,80})\r?$", self.output,
            ) if code.decode("ascii") in NPM_ERRORS
        })[:4]
        return {
            "status": self.status, "exitCode": self.exit_code, "reason": self.reason,
            "capturedOutputBytes": len(self.output), "capturedOutputSha256": sha256(self.output),
            "npmErrorCodes": codes,
        }


class _BasicJobLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedJobLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicJobLimits), ("IoInfo", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JobAccounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32), ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32), ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32), ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p), ("lpTitle", ctypes.c_wchar_p),
        *[(name, ctypes.c_uint32) for name in (
            "dwX", "dwY", "dwXSize", "dwYSize", "dwXCountChars", "dwYCountChars",
            "dwFillAttribute", "dwFlags",
        )],
        ("wShowWindow", ctypes.c_uint16), ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.c_void_p), ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p), ("hStdError", ctypes.c_void_p),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32), ("dwThreadId", ctypes.c_uint32),
    ]


class _WindowsAPI:
    def __init__(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        pointer = ctypes.c_void_p
        dword = ctypes.c_uint32
        boolean = ctypes.c_int32

        def bind(name, result, *arguments):
            function = getattr(kernel, name)
            function.restype = result
            function.argtypes = arguments
            return function

        self.create_job = bind("CreateJobObjectW", pointer, pointer, ctypes.c_wchar_p)
        self.set_job = bind("SetInformationJobObject", boolean, pointer, dword, pointer, dword)
        self.query_job = bind("QueryInformationJobObject", boolean, pointer, dword, pointer, dword, pointer)
        self.terminate_job = bind("TerminateJobObject", boolean, pointer, dword)
        self.initialize_attributes = bind(
            "InitializeProcThreadAttributeList", boolean, pointer, dword, dword, ctypes.POINTER(ctypes.c_size_t),
        )
        self.update_attribute = bind(
            "UpdateProcThreadAttribute", boolean, pointer, dword, ctypes.c_size_t, pointer,
            ctypes.c_size_t, pointer, pointer,
        )
        self.delete_attributes = bind("DeleteProcThreadAttributeList", None, pointer)
        self.create_process = bind(
            "CreateProcessW", boolean, ctypes.c_wchar_p, ctypes.c_wchar_p, pointer, pointer,
            boolean, dword, pointer, ctypes.c_wchar_p, ctypes.POINTER(_StartupInfoEx),
            ctypes.POINTER(_ProcessInformation),
        )
        self.close_handle = bind("CloseHandle", boolean, pointer)
        self.wait = bind("WaitForSingleObject", dword, pointer, dword)
        self.exit_code = bind("GetExitCodeProcess", boolean, pointer, ctypes.POINTER(dword))


class _WindowsJobProcess:
    """JOB_LIST enrollment happens inside CreateProcessW, before any child code runs."""

    def __init__(self, command: list[str], cwd: Path, env: dict[str, str]):
        import msvcrt

        self.api = _WindowsAPI()
        self.job = None
        self.handle = None
        self.stdout = None
        self.returncode: int | None = None
        self.pid = 0
        self.args = command
        info = _ProcessInformation()
        read_fd = write_fd = input_fd = None
        attributes = None
        ready = False
        try:
            self.job = self.api.create_job(None, None)
            require(bool(self.job), "command_ownership_failed")
            limits = _ExtendedJobLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            require(bool(self.api.set_job(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits))),
                    "command_ownership_failed")
            read_fd, write_fd = os.pipe()
            input_fd = os.open(os.devnull, os.O_RDONLY)
            for descriptor in (write_fd, input_fd):
                os.set_inheritable(descriptor, True)
            inherited = (ctypes.c_void_p * 2)(
                msvcrt.get_osfhandle(input_fd), msvcrt.get_osfhandle(write_fd),
            )
            jobs = (ctypes.c_void_p * 1)(self.job)
            size = ctypes.c_size_t()
            ctypes.set_last_error(0)
            self.api.initialize_attributes(None, 2, 0, ctypes.byref(size))
            require(ctypes.get_last_error() == 122 and size.value > 0, "command_ownership_failed")
            buffer = ctypes.create_string_buffer(size.value)
            require(bool(self.api.initialize_attributes(buffer, 2, 0, ctypes.byref(size))),
                    "command_ownership_failed")
            attributes = buffer
            for attribute, values in (
                (0x20002, inherited),  # PROC_THREAD_ATTRIBUTE_HANDLE_LIST
                (0x2000D, jobs),       # PROC_THREAD_ATTRIBUTE_JOB_LIST
            ):
                require(bool(self.api.update_attribute(
                    attributes, 0, attribute, values, ctypes.sizeof(values), None, None,
                )), "command_ownership_failed")
            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = inherited[0]
            startup.StartupInfo.hStdOutput = inherited[1]
            startup.StartupInfo.hStdError = inherited[1]
            startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
            executable = shutil.which(command[0], path=env.get("PATH"))
            if executable is None:
                raise FileNotFoundError("command_executable_missing")
            environment = ctypes.create_unicode_buffer(
                "\0".join(f"{name}={env[name]}" for name in sorted(env, key=str.upper)) + "\0",
            )
            commandline = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
            require(bool(self.api.create_process(
                executable, commandline, None, None, True,
                0x80000 | 0x400 | 0x200,  # EXTENDED_STARTUPINFO_PRESENT, UNICODE_ENVIRONMENT, NEW_PROCESS_GROUP
                environment, str(cwd), ctypes.byref(startup), ctypes.byref(info),
            )), "command_start_failed")
            self.handle = info.hProcess
            self.pid = info.dwProcessId
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            read_fd = None
            ready = True
        finally:
            if attributes is not None:
                self.api.delete_attributes(attributes)
            cleanup_failed = bool(info.hThread) and not self.api.close_handle(info.hThread)
            for descriptor in (read_fd, write_fd, input_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        cleanup_failed = True
            if not ready or cleanup_failed:
                self.close()
            require(not cleanup_failed, "command_cleanup_unknown")

    def poll(self):
        if self.returncode is None:
            state = self.api.wait(self.handle, 0)
            require(state in (0, 258), "command_cleanup_unknown")
            if state == 0:
                code = ctypes.c_uint32()
                require(bool(self.api.exit_code(self.handle, ctypes.byref(code))), "command_cleanup_unknown")
                self.returncode = code.value
        return self.returncode

    def wait(self, timeout: float):
        require(self.api.wait(self.handle, max(0, int(timeout * 1000))) == 0, "command_cleanup_unknown")
        return self.poll()

    def active_processes(self):
        info = _JobAccounting()
        require(bool(self.api.query_job(self.job, 1, ctypes.byref(info), ctypes.sizeof(info), None)),
                "command_cleanup_unknown")
        return info.ActiveProcesses

    def terminate_tree(self):
        require(bool(self.api.terminate_job(self.job, 1)), "command_cleanup_unknown")
        deadline = time.monotonic() + 10
        self.wait(10)
        while self.active_processes():
            require(time.monotonic() < deadline, "command_cleanup_unknown")
            time.sleep(0.01)

    def close(self):
        try:
            if self.stdout is not None:
                self.stdout.close()
        finally:
            handles = (self.job, self.handle)
            self.job = self.handle = None
            failures = [handle for handle in handles if handle and not self.api.close_handle(handle)]
            require(not failures, "command_cleanup_unknown")


def launch_process(command: list[str], cwd: Path, env: dict[str, str]):
    if os.name == "nt":
        return _WindowsJobProcess(command, cwd, env)
    return subprocess.Popen(
        command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0, start_new_session=True,
    )


def active_descendants(process) -> bool:
    if os.name == "nt":
        require(isinstance(process, _WindowsJobProcess), "command_ownership_failed")
        return process.active_processes() > 0
    try:
        os.killpg(process.pid, 0)
        return True
    except ProcessLookupError:
        return False


def stop_tree(process) -> None:
    if os.name == "nt":
        require(isinstance(process, _WindowsJobProcess), "command_ownership_failed")
        process.terminate_tree()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


def close_process(process) -> None:
    try:
        process.stdout.close()
    finally:
        if isinstance(process, _WindowsJobProcess):
            process.close()


def run_process(command: list[str], cwd: Path, env: dict[str, str], timeout: float,
                limit: int = OUTPUT_LIMIT) -> CommandResult:
    if timeout <= 0:
        return CommandResult("not_run", None, reason="total_deadline")
    output = bytearray()
    try:
        process = launch_process(command, cwd, env)
    except OSError:
        return CommandResult("failed", None, reason="command_start_failed")
    stream = process.stdout
    assert stream is not None
    deadline = time.monotonic() + timeout
    reason = None

    def capture(chunk: bytes):
        nonlocal reason
        remaining = limit - len(output)
        output.extend(chunk[:remaining])
        if len(chunk) > remaining:
            reason = "output_limit"

    try:
        try:
            # Python 3.12+ supports nonblocking anonymous pipes on Windows.
            # No reader thread can outlive the owned command or retain its pipe.
            os.set_blocking(stream.fileno(), False)
            while process.poll() is None:
                chunk = stream.read(65536)
                if chunk:
                    capture(chunk)
                if reason or time.monotonic() >= deadline:
                    reason = reason or "command_timeout"
                    break
                if not chunk:
                    time.sleep(0.01)
            if reason is None and active_descendants(process):
                reason = "command_descendants_running"
        finally:
            try:
                # The Job Object/process group outlives its leader. Always
                # terminate it, even after exit 0 and even if stdout is closed.
                stop_tree(process)
                drain_deadline = time.monotonic() + 2
                while True:
                    require(time.monotonic() < drain_deadline, "command_cleanup_unknown")
                    chunk = stream.read(65536)
                    if chunk == b"":
                        break
                    if chunk:
                        capture(chunk)
                    else:
                        time.sleep(0.01)
            finally:
                close_process(process)
        status = "succeeded" if process.returncode == 0 and reason is None else "failed"
        return CommandResult(status, process.returncode, bytes(output), reason)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DiagnosticError("command_cleanup_unknown") from exc


def metadata_url(name: str) -> str:
    return f"{REGISTRY}/{name.replace('/', '%2f')}/{VERSION}"


def tarball_url(name: str) -> str:
    return f"{REGISTRY}/{name}/-/{name.rsplit('/', 1)[-1]}-{VERSION}.tgz"


def download(url: str, destination: Path, limit: int) -> bytes:
    approved = {NODE_INDEX}
    for name in ("next", NATIVE):
        approved.update((metadata_url(name), tarball_url(name)))
    require(url in approved, "unapproved_public_url")
    parsed = urlsplit(url)
    digest = hashlib.sha512()
    total = 0
    deadline = time.monotonic() + 30
    connection = http.client.HTTPSConnection(parsed.netloc, timeout=10)
    try:
        connection.request("GET", parsed.path, headers={
            "Accept-Encoding": "identity", "Cache-Control": "no-cache",
            "User-Agent": "AI4IA-PR477-native-diagnostic/1",
        })
        response = connection.getresponse()
        if response.status != 200:
            raise DiagnosticError("public_http_status", response.status)
        require(
            response.headers.get_all("Content-Encoding", []) in ([], ["identity"])
            and not response.headers.get_all("Content-Range", []),
            "encoded_or_partial_download",
        )
        lengths = response.headers.get_all("Content-Length", [])
        require(
            not lengths or (
                len(lengths) == 1 and lengths[0].isdigit() and 0 < int(lengths[0]) <= limit
            ),
            "invalid_download_length",
        )
        with destination.open("xb") as stream:
            while True:
                require(time.monotonic() < deadline, "download_deadline")
                chunk = response.read(min(65536, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                require(total <= limit, "download_size_limit")
                stream.write(chunk)
                digest.update(chunk)
        require(total > 0 and (not lengths or total == int(lengths[0])), "incomplete_download")
        return digest.digest()
    except ssl.SSLError as exc:
        raise DiagnosticError("public_tls_failure") from exc
    except socket.gaierror as exc:
        raise DiagnosticError("public_dns_failure") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise DiagnosticError("public_transport_unavailable") from exc
    finally:
        connection.close()


def verify_metadata(name: str, value: dict, lock: dict) -> dict:
    require(value.get("name") == name and value.get("version") == VERSION, "package_identity_mismatch")
    dist = value.get("dist")
    if not isinstance(dist, dict):
        raise DiagnosticError("package_dist_missing")
    integrity = dist.get("integrity", "")
    require(
        isinstance(integrity, str) and bool(re.fullmatch(r"sha512-[A-Za-z0-9+/]{86}==", integrity))
        and dist.get("tarball") == tarball_url(name),
        "invalid_package_dist",
    )
    digest = base64.b64decode(integrity[7:], validate=True)
    require(len(digest) == 64 and base64.b64encode(digest).decode() == integrity[7:], "invalid_sri")
    selected = {
        "name": name, "version": VERSION,
        "dist": {"tarball": dist["tarball"], "integrity": integrity},
    }
    if name == "next":
        locked = lock["packages"]["node_modules/next"]
        require(
            locked["resolved"] == dist["tarball"] and locked["integrity"] == integrity
            and isinstance(value.get("optionalDependencies"), dict)
            and encode(value["optionalDependencies"]) == encode(locked["optionalDependencies"])
            and value["optionalDependencies"].get(NATIVE) == VERSION,
            "next_lock_metadata_mismatch",
        )
        selected["optionalDependencies"] = value["optionalDependencies"]
    else:
        require(value.get("os") == ["win32"] and value.get("cpu") == ["x64"], "native_platform_mismatch")
        require(isinstance(value.get("license"), str) and 0 < len(value["license"]) <= 64,
                "native_license_missing")
        require(
            isinstance(value.get("engines"), dict) and set(value["engines"]) == {"node"}
            and isinstance(value["engines"]["node"], str) and len(value["engines"]["node"]) <= 128,
            "native_engines_missing",
        )
        selected.update({key: value[key] for key in ("os", "cpu", "license", "engines")})
    return selected


@contextmanager
def bounded_tar_stream(path: Path):
    with gzip.open(path, "rb") as compressed, tempfile.TemporaryFile(dir=path.parent) as uncompressed:
        total = 0
        while chunk := compressed.read(min(65536, UNPACKED_LIMIT - total + 1)):
            total += len(chunk)
            require(total <= UNPACKED_LIMIT, "package_archive_limit")
            uncompressed.write(chunk)
        uncompressed.seek(0)
        yield uncompressed


def inspect_tarball(path: Path, metadata: dict) -> dict:
    manifest = None
    binaries = {}
    total = 0
    count = 0
    try:
        with bounded_tar_stream(path) as uncompressed, tarfile.open(fileobj=uncompressed, mode="r|") as archive:
            for member in archive:
                count += 1
                total += member.size
                require(count <= 20000 and total <= UNPACKED_LIMIT, "package_archive_limit")
                require(member.isdir() or member.isfile(), "package_archive_link")
                if not member.isfile():
                    continue
                if member.name == "package/package.json" or (
                    metadata["name"] == NATIVE and member.name.endswith(".node")
                ):
                    stream = archive.extractfile(member)
                    require(stream is not None, "package_member_missing")
                    assert stream is not None
                    if member.name == "package/package.json":
                        require(manifest is None and member.size <= REPORT_LIMIT, "package_manifest_limit")
                        manifest = object_json(stream.read(REPORT_LIMIT + 1))
                    else:
                        require(member.name.count("/") == 1, "nested_native_binary")
                        require(member.name not in binaries, "duplicate_native_binary")
                        require(member.size > 0, "empty_native_binary")
                        digest = hashlib.sha256()
                        size = 0
                        while chunk := stream.read(65536):
                            size += len(chunk)
                            require(size <= member.size, "native_member_size")
                            digest.update(chunk)
                        require(size == member.size, "native_member_truncated")
                        binaries[member.name] = digest.hexdigest()
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise DiagnosticError("invalid_package_archive") from exc
    if not isinstance(manifest, dict):
        raise DiagnosticError("package_manifest_missing")
    require(
        manifest.get("name") == metadata["name"] and manifest.get("version") == VERSION,
        "tarball_identity_mismatch",
    )
    if metadata["name"] == NATIVE:
        require(
            manifest.get("os") == ["win32"] and manifest.get("cpu") == ["x64"]
            and len(binaries) == 1 and f"package/{manifest.get('main')}" in binaries
            and isinstance(manifest.get("scripts", {}), dict)
            and not any(key in manifest.get("scripts", {}) for key in ("preinstall", "install", "postinstall")),
            "tarball_native_contract",
        )
        return {"binarySha256": next(iter(binaries.values()))}
    require(
        encode(manifest.get("optionalDependencies")) == encode(metadata["optionalDependencies"]),
        "tarball_next_contract",
    )
    return {}


def collect_public(work: Path, lock: dict, result: dict | None = None) -> dict:
    result = result if result is not None else {}
    result.update({
        "node": NODE_VERSION, "npm": NPM_VERSION, "nodeMetadataSource": NODE_INDEX, "packages": {},
        "operations": {name: {"status": "not_run"} for name in PUBLIC_OPERATIONS},
    })
    active = "node_release"

    def fetch(url: str, path: Path, limit: int):
        result["operations"][active] = {"status": "running", "url": url}
        digest = download(url, path, limit)
        result["operations"][active].update(bytes=path.stat().st_size, sha512=base64.b64encode(digest).decode())
        return digest

    try:
        release_path = work / "node-index.json"
        fetch(NODE_INDEX, release_path, 2 * LOCK_LIMIT)
        releases = strict_json(read_bytes(release_path, 2 * LOCK_LIMIT))
        require(isinstance(releases, list), "node_release_index_invalid")
        matching = [row for row in releases if isinstance(row, dict) and row.get("version") == f"v{NODE_VERSION}"]
        require(
            len(matching) == 1 and matching[0].get("npm") == NPM_VERSION
            and isinstance(matching[0].get("files"), list)
            and "win-x64-zip" in matching[0].get("files", []),
            "node_bundled_npm_mismatch",
        )
        result["operations"][active]["status"] = "succeeded"
        for index, name in enumerate(("next", NATIVE)):
            prefix = "next" if name == "next" else "native"
            active = f"{prefix}_metadata"
            path = work / f"metadata-{index}.json"
            fetch(metadata_url(name), path, LOCK_LIMIT)
            metadata = verify_metadata(name, object_json(read_bytes(path, LOCK_LIMIT)), lock)
            result["operations"][active]["status"] = "succeeded"
            active = f"{prefix}_tarball"
            tarball = work / f"package-{index}.tgz"
            actual = fetch(metadata["dist"]["tarball"], tarball, TARBALL_LIMIT)
            require(
                "sha512-" + base64.b64encode(actual).decode() == metadata["dist"]["integrity"],
                "tarball_integrity_mismatch",
            )
            metadata.update(inspect_tarball(tarball, metadata))
            metadata["tarballBytes"] = tarball.stat().st_size
            result["packages"][name] = metadata
            result["operations"][active]["status"] = "succeeded"
    except (DiagnosticError, OSError) as exc:
        result["operations"][active].update(
            status="failed", reason=str(exc) if isinstance(exc, DiagnosticError) else "public_worker_io_failure",
            httpStatus=exc.http_status if isinstance(exc, DiagnosticError) else None,
        )
        raise
    return result


def verify_operations(operations: dict) -> None:
    expected = {
        "node_release": (NODE_INDEX, 2 * LOCK_LIMIT),
        "next_metadata": (metadata_url("next"), LOCK_LIMIT),
        "next_tarball": (tarball_url("next"), TARBALL_LIMIT),
        "native_metadata": (metadata_url(NATIVE), LOCK_LIMIT),
        "native_tarball": (tarball_url(NATIVE), TARBALL_LIMIT),
    }
    require(set(operations) == set(expected), "public_operation_inventory")
    for name, value in operations.items():
        require(isinstance(value, dict), "public_operation_shape")
        if value == {"status": "not_run"}:
            continue
        require(
            value.get("status") in {"succeeded", "failed"} and value.get("url") == expected[name][0]
            and not (set(value) - {"status", "url", "bytes", "sha512", "reason", "httpStatus"}),
            "public_operation_shape",
        )
        if value["status"] == "succeeded" or "bytes" in value:
            require(
                type(value.get("bytes")) is int and 0 < value["bytes"] <= expected[name][1]
                and isinstance(value.get("sha512"), str)
                and bool(re.fullmatch(r"[A-Za-z0-9+/]{86}==", value["sha512"])),
                "public_operation_digest_missing",
            )
        if value["status"] == "failed":
            require(
                isinstance(value.get("reason"), str)
                and bool(re.fullmatch("[a-z_]{1,64}", value["reason"]))
                and (value.get("httpStatus") is None or (
                    type(value["httpStatus"]) is int and 100 <= value["httpStatus"] <= 599
                )),
                "public_operation_error_shape",
            )


def extract_source(data: bytes, destination: Path) -> None:
    total = 0
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            require(
                not name.is_absolute() and ".." not in name.parts and "\\" not in member.name
                and member.name not in seen and len(seen) < 4096
                and (member.isdir() or member.isfile()),
                "unsafe_source_archive",
            )
            seen.add(member.name)
            require(
                member.name in {"app", "app/api", "app/api/tests", "app/web"}
                or member.name.startswith("app/web/")
                or member.name == "app/api/tests/citation_contract.json",
                "unexpected_source_path",
            )
            require(
                not any(part in {"node_modules", ".npmrc"} for part in name.parts)
                and not (name.name.startswith(".env") and name.name != ".env.example"),
                "unexpected_source_configuration",
            )
            total += member.size
            require(total <= SOURCE_LIMIT, "source_archive_limit")
            target = destination.joinpath(*name.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                require(stream is not None, "source_member_missing")
                assert stream is not None
                with target.open("xb") as output:
                    shutil.copyfileobj(stream, output, 65536)


def export_canonical_source(git: Callable[..., bytes], destination: Path) -> None:
    # git archive applies checkout conversion unless its EOL settings are scoped.
    extract_source(git(
        "-c", "core.autocrlf=false", "-c", "core.eol=lf",
        "archive", "--format=tar", "HEAD", "app/web", "app/api/tests/citation_contract.json",
        limit=SOURCE_LIMIT,
    ), destination)


def source_inputs(web: Path, evidence: dict) -> dict[str, bytes]:
    paths = {
        "package.json": web / "package.json",
        "package-lock.json": web / "package-lock.json",
        "nativeLockCoverage.test.ts": web / "src" / "lib" / "nativeLockCoverage.test.ts",
    }
    evidence.update({
        name: {"present": path.is_file(), "bytes": None, "sha256": None}
        for name, path in paths.items()
    })
    manifests = {}
    for name, path in paths.items():
        if not evidence[name]["present"]:
            continue
        require(not path.is_symlink(), "regular_file_required")
        limit = LOCK_LIMIT if name in ORIGINAL_HASHES else SOURCE_LIMIT
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        require(len(data) <= limit, "file_size_limit")
        evidence[name].update(bytes=len(data), sha256=sha256(data))
        if name in ORIGINAL_HASHES:
            manifests[name] = data
    for name, prefix in (("package.json", "source_manifest"), ("package-lock.json", "source_lock")):
        require(evidence[name]["present"], f"{prefix}_missing")
        require(evidence[name]["sha256"] == ORIGINAL_HASHES[name], f"{prefix}_hash_mismatch")
    require(evidence["nativeLockCoverage.test.ts"]["present"], "source_native_guard_missing")
    return manifests


def candidate_inventory(original_lock: bytes, candidate_lock: bytes) -> dict:
    if max(len(original_lock), len(candidate_lock)) > LOCK_LIMIT:
        return {"status": "unavailable", "reason": "candidate_inventory_input_limit"}
    try:
        original = object_json(original_lock)
        candidate = object_json(candidate_lock)
        before = original.get("packages")
        after = candidate.get("packages")
        if not isinstance(before, dict) or not isinstance(after, dict):
            return {"status": "unavailable", "reason": "candidate_inventory_packages_missing"}
        before_keys, after_keys = set(before), set(after)
        allowlist = before_keys | {NATIVE_KEY}

        def changes(paths: set[str]) -> dict:
            known = paths & allowlist
            displayed = sorted(
                path for path in known if path.startswith("node_modules/")
                and len(path) <= 256 and re.fullmatch(r"[A-Za-z0-9@._/-]+", path)
            )[:16]
            return {
                "count": len(paths), "knownCount": len(known), "unknownCount": len(paths - known),
                "knownPaths": displayed, "omittedKnownPaths": len(known) - len(displayed),
            }

        return {
            "status": "observed",
            "beforeRecordCount": len(before_keys), "afterRecordCount": len(after_keys),
            "beforeNativePresent": NATIVE_KEY in before_keys, "afterNativePresent": NATIVE_KEY in after_keys,
            "bytesUnchanged": original_lock == candidate_lock,
            "topLevelUnchanged": encode({key: value for key, value in original.items() if key != "packages"})
            == encode({key: value for key, value in candidate.items() if key != "packages"}),
            "rootRecordUnchanged": encode(before.get("")) == encode(after.get("")),
            "added": changes(after_keys - before_keys),
            "removed": changes(before_keys - after_keys),
            "changed": changes({
                key for key in before_keys & after_keys if encode(before[key]) != encode(after[key])
            }),
        }
    except (DiagnosticError, UnicodeError):
        return {"status": "unavailable", "reason": "candidate_inventory_invalid_json"}


def verify_candidate(original_manifest: bytes, original_lock: bytes, candidate_manifest: bytes,
                     candidate_lock: bytes, native: dict) -> None:
    require(candidate_manifest == original_manifest, "candidate_manifest_changed")
    original = object_json(original_lock)
    candidate = object_json(candidate_lock)
    require(isinstance(original.get("packages"), dict) and isinstance(candidate.get("packages"), dict),
            "candidate_packages_missing")
    original_packages = original.pop("packages")
    candidate_packages = candidate.pop("packages")
    require(
        encode(original) == encode(candidate)
        and set(candidate_packages) == set(original_packages) | {NATIVE_KEY}
        and NATIVE_KEY not in original_packages,
        "candidate_lock_inventory_changed",
    )
    for path, entry in original_packages.items():
        require(encode(entry) == encode(candidate_packages[path]), "existing_lock_record_changed")
    expected = {
        "version": VERSION, "resolved": native["dist"]["tarball"],
        "integrity": native["dist"]["integrity"], "optional": True,
        **{key: native[key] for key in ("cpu", "os", "license", "engines")},
    }
    require(encode(candidate_packages[NATIVE_KEY]) == encode(expected), "candidate_native_record_mismatch")
    text_control = object_json(candidate_lock)
    require(candidate_lock == npm_json(text_control), "candidate_format_changed")
    del text_control["packages"][NATIVE_KEY]
    require(original_lock == npm_json(text_control), "existing_lock_text_changed")


def project_generated_native(original_manifest: bytes, original_lock: bytes, raw_manifest: bytes,
                             raw_lock: bytes, native: dict) -> bytes:
    require(max(map(len, (original_manifest, original_lock, raw_manifest, raw_lock))) <= LOCK_LIMIT,
            "projection_input_limit")
    require(raw_manifest == original_manifest, "raw_candidate_manifest_changed")
    original = object_json(original_lock)
    raw = object_json(raw_lock)
    before, generated = original.get("packages"), raw.get("packages")
    require(isinstance(before, dict) and isinstance(generated, dict), "raw_candidate_packages_missing")
    assert isinstance(before, dict) and isinstance(generated, dict)
    require(NATIVE_KEY not in before and NATIVE_KEY in generated, "raw_candidate_native_inventory")
    require(set(generated) == set(before) | {NATIVE_KEY}, "raw_candidate_inventory_changed")
    require(
        encode({key: value for key, value in original.items() if key != "packages"})
        == encode({key: value for key, value in raw.items() if key != "packages"}),
        "raw_candidate_top_level_changed",
    )
    require(encode(generated.get("")) == encode(before.get("")), "raw_candidate_root_changed")
    before[NATIVE_KEY] = generated[NATIVE_KEY]
    projected = npm_json(original)
    require(len(projected) <= LOCK_LIMIT, "projection_output_limit")
    verify_candidate(original_manifest, original_lock, raw_manifest, projected, native)
    return projected


def verify_native(output: bytes, native: dict) -> dict:
    value = object_json(output)
    require(encode(value) == encode({
        "native": True, "node": NODE_VERSION, "platform": "win32", "arch": "x64",
        "name": NATIVE, "version": VERSION, "binarySha256": native["binarySha256"], "result": 42,
    }), "native_evidence_mismatch")
    return value


class Diagnostic:
    def __init__(self, output: Path, env: dict[str, str]):
        self.output = output
        self.env = env
        self.deadline = time.monotonic() + TOTAL_SECONDS
        self.active_stage: str | None = None
        self.immutable: dict[Path, bytes] = {}
        self.control_env: dict[str, str] | None = None
        self.report = {
            "schemaVersion": 1, "status": "blocked", "adoptionAuthorized": False,
            "candidateValidated": False, "originalValidated": False,
            "originalCanonicalHashes": ORIGINAL_HASHES,
            "commands": [], "errors": [],
            "publicOperations": {name: {"status": "not_run"} for name in PUBLIC_OPERATIONS},
            "stages": {name: {"status": "not_run", "exitCode": None, "reason": "prerequisite_not_met"}
                       for name in STAGES},
        }

    def save(self):
        data = encode(self.report)
        require(len(data) <= REPORT_LIMIT, "report_size_limit")
        (self.output / "report.json").write_bytes(data)

    def command(self, stage: str, args: list[str], cwd: Path, env: dict[str, str],
                seconds: int, limit: int = OUTPUT_LIMIT) -> CommandResult:
        self.active_stage = stage
        require(len(self.report["commands"]) < 48, "command_count_limit")
        self.report["stages"][stage] = {"status": "running", "exitCode": None, "reason": None}
        self.save()
        reserve = 0 if stage == "immutability" else 15
        try:
            result = run_process(args, cwd, env, min(seconds, self.deadline - time.monotonic() - reserve), limit)
        except DiagnosticError as exc:
            result = CommandResult("failed", None, reason=str(exc))
            self.report["stages"][stage] = result.summary()
            self.report["commands"].append({"stage": stage, **result.summary()})
            self.save()
            raise
        self.report["stages"][stage] = result.summary()
        self.report["commands"].append({"stage": stage, **result.summary()})
        self.save()
        return result

    def success(self, stage: str, evidence: dict | None = None):
        self.report["stages"][stage] = {
            "status": "succeeded", "exitCode": None, "reason": None, "evidence": evidence or {},
        }
        self.active_stage = None
        self.save()

    def failure(self, reason: str):
        self.report["errors"].append({"stage": self.active_stage, "reason": reason})
        if self.active_stage:
            self.report["stages"][self.active_stage].update(status="failed", reason=reason)
        self.report["status"] = "blocked"
        self.report["candidateValidated"] = False
        self.report["originalValidated"] = False

    def finish(self):
        if self.immutable and self.control_env is not None:
            self.active_stage = "immutability"
            require(
                all(read_bytes(path, LOCK_LIMIT) == data for path, data in self.immutable.items()),
                "original_or_candidate_mutated",
            )
            clean = self.command("immutability", [
                "git", "status", "--porcelain", "--untracked-files=all",
            ], ROOT, self.control_env, 15)
            require(clean.status == "succeeded" and not clean.output.strip(), "checkout_modified")
            self.success("immutability")
        if not self.report["errors"]:
            for kind in ("original", "candidate"):
                self.report[f"{kind}Validated"] = all(
                    value["status"] == "succeeded" for name, value in self.report["stages"].items()
                    if name.startswith(f"{kind}_")
                ) and self.report["stages"]["immutability"]["status"] == "succeeded"
            self.report["status"] = (
                "both_verified" if self.report["originalValidated"] and self.report["candidateValidated"]
                else "candidate_verified_original_failed" if self.report["candidateValidated"] else "blocked"
            )

    def run(self, work: Path) -> None:
        self.active_stage = "source"
        self.report["identity"] = identity(self.env)
        clean_env = child_env(self.env, work / "control-home")
        self.control_env = clean_env

        def git(*args: str, limit: int = OUTPUT_LIMIT) -> bytes:
            result = self.command("source", ["git", *args], ROOT, clean_env, 30, limit)
            require(result.status == "succeeded", "source_git_failed")
            return result.output

        require(git("rev-parse", "HEAD").decode().strip() == self.report["identity"]["sourceCommit"],
                "checkout_source_mismatch")
        git("merge-base", "--is-ancestor", APPROVED_MAIN, "HEAD")
        git("merge-base", "--is-ancestor", ORIGINAL, "HEAD")
        require(not git("status", "--porcelain", "--untracked-files=all").strip(), "checkout_not_clean")
        self.immutable = {
            ROOT / "app" / "web" / name: read_bytes(ROOT / "app" / "web" / name, LOCK_LIMIT)
            for name in ORIGINAL_HASHES
        }
        checkout_hashes = {path.name: sha256(data) for path, data in self.immutable.items()}
        source = work / "source"
        export_canonical_source(git, source)
        web = source / "app" / "web"
        self.report["sourceInputs"] = {}
        manifests = source_inputs(web, self.report["sourceInputs"])
        lock = object_json(manifests["package-lock.json"])
        require(NATIVE_KEY not in lock["packages"], "original_missing_record_precondition")
        self.success("source", {"checkoutDependencyHashes": checkout_hashes})

        self.active_stage = "toolchain"
        node = shutil.which("node", path=clean_env.get("PATH"))
        require(node is not None, "node_missing")
        assert node is not None
        npm = Path(node).parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        require(npm.is_file(), "bundled_npm_missing")
        npm_manifest = read_bytes(npm.parent.parent / "package.json", REPORT_LIMIT)
        npm_package = object_json(npm_manifest)
        require(npm_package.get("name") == "npm" and npm_package.get("version") == NPM_VERSION,
                "bundled_npm_manifest_mismatch")
        version = self.command("toolchain", [node, "-p", (
            "JSON.stringify({node:process.versions.node,platform:process.platform,arch:process.arch})"
        )], work, clean_env, 15)
        require(version.status == "succeeded" and object_json(version.output) == {
            "node": NODE_VERSION, "platform": "win32", "arch": "x64",
        }, "node_runtime_mismatch")
        npm_version = self.command("toolchain", [node, str(npm), "--version"], work, clean_env, 15)
        require(npm_version.status == "succeeded" and npm_version.output.strip() == NPM_VERSION.encode(),
                "bundled_npm_runtime_mismatch")
        self.success("toolchain", {
            "node": NODE_VERSION, "npm": NPM_VERSION, "bundledNpmManifestSha256": sha256(npm_manifest),
        })
        collect_dir = work / "public"
        collect_dir.mkdir()
        worker_env = {**clean_env, **{name: self.env[name] for name in IDENTITY_ENV},
                      "RUNNER_TEMP": self.env["RUNNER_TEMP"]}
        self.report["publicOperations"] = {name: {"status": "unknown"} for name in PUBLIC_OPERATIONS}
        public = self.command("public_packages", [
            sys.executable, str(Path(__file__).resolve()), "--collect", str(collect_dir),
            "--lock", str(web / "package-lock.json"),
        ], work, worker_env, 120, REPORT_LIMIT)
        metadata = object_json(public.output) if public.output else {}
        operations = metadata.get("operations", {})
        if isinstance(operations, dict) and set(operations) == set(PUBLIC_OPERATIONS):
            verify_operations(operations)
            self.report["publicOperations"] = operations
        if public.status != "succeeded":
            self.report["stages"]["public_packages"]["reason"] = "public_evidence_unavailable"
        require(public.status == "succeeded", "public_evidence_unavailable")
        require(
            metadata.get("node") == NODE_VERSION and metadata.get("npm") == NPM_VERSION
            and metadata.get("nodeMetadataSource") == NODE_INDEX
            and isinstance(metadata.get("packages"), dict)
            and set(metadata["packages"]) == {"next", NATIVE}
            and all(value.get("status") == "succeeded" for value in operations.values())
            and set(operations) == set(PUBLIC_OPERATIONS),
            "public_worker_result_invalid",
        )
        for name, package in metadata["packages"].items():
            verify_metadata(name, package, lock)
        require(
            isinstance(metadata["packages"][NATIVE].get("binarySha256"), str)
            and bool(re.fullmatch("[0-9a-f]{64}", metadata["packages"][NATIVE]["binarySha256"])),
            "native_binary_hash_missing",
        )
        self.report["publicPackages"] = metadata
        native = metadata["packages"][NATIVE]
        self.save()

        copies = {}
        environments = {}
        for kind in ("original", "candidate"):
            target = work / kind
            shutil.copytree(source, target)
            copies[kind] = target / "app" / "web"
            environments[kind] = child_env(self.env, work / f"{kind}-home")
        original = copies["original"]
        candidate = copies["candidate"]
        self.immutable.update({original / name: data for name, data in manifests.items()})
        self.immutable[candidate / "package.json"] = manifests["package.json"]

        def invoke(kind: str, suffix: str, args: list[str], seconds: int) -> CommandResult:
            return self.command(f"{kind}_{suffix}", args, copies[kind], environments[kind], seconds)

        def native_control(kind: str):
            result = invoke(kind, "native", [
                node, str(ROOT / "scripts" / "_web_native_probe.cjs"),
                str(copies[kind]), native["binarySha256"],
            ], 30)
            if result.status == "succeeded":
                self.report["stages"][f"{kind}_native"]["evidence"] = verify_native(result.output, native)
                self.save()

        installed = invoke("original", "install", [node, str(npm), "ci", *NPM_FLAGS], 300)
        vitest = str(Path("node_modules") / "vitest" / "vitest.mjs")
        guard = str(Path("src") / "lib" / "nativeLockCoverage.test.ts")
        if installed.status == "succeeded":
            invoke("original", "guard", [node, vitest, "run", guard], 60)
            native_control("original")

        self.active_stage = "candidate_lock"
        require(
            read_bytes(candidate / "package.json", LOCK_LIMIT) == manifests["package.json"]
            and read_bytes(candidate / "package-lock.json", LOCK_LIMIT) == manifests["package-lock.json"],
            "candidate_refresh_inputs_changed",
        )
        dependencies = object_json(manifests["package.json"]).get("dependencies")
        require(
            isinstance(dependencies, dict) and dependencies.get("next") == VERSION
            and metadata["packages"]["next"]["name"] == "next"
            and metadata["packages"]["next"]["version"] == VERSION,
            "candidate_refresh_parent_mismatch",
        )
        generated = invoke("candidate", "lock", [
            node, str(npm), "install", f"next@{VERSION}", "--save-exact", "--package-lock-only", *NPM_FLAGS,
        ], 120)
        require(generated.status == "succeeded", "candidate_generation_failed")
        self.active_stage = "candidate_projection"
        raw_lock = read_bytes(candidate / "package-lock.json", LOCK_LIMIT)
        raw_manifest = read_bytes(candidate / "package.json", LOCK_LIMIT)
        raw_inventory = candidate_inventory(manifests["package-lock.json"], raw_lock)
        self.report["rawGeneration"] = {
            "accepted": False, "lockSha256": sha256(raw_lock),
            "manifestSha256": sha256(raw_manifest), "inventory": raw_inventory,
        }
        self.save()
        require(raw_inventory["status"] == "observed", "raw_candidate_inventory_unavailable")
        projected_lock = project_generated_native(
            manifests["package.json"], manifests["package-lock.json"], raw_manifest, raw_lock, native,
        )
        raw_native = object_json(raw_lock)["packages"][NATIVE_KEY]
        projected_native = object_json(projected_lock)["packages"][NATIVE_KEY]
        require(encode(raw_native) == encode(projected_native), "native_projection_changed")
        self.report["repairedCandidate"] = {
            "lockSha256": sha256(projected_lock),
            "inventory": candidate_inventory(manifests["package-lock.json"], projected_lock),
            "nativeRecordSource": "actual_npm_generated_record",
            "rawNativeRecordSha256": sha256(encode(raw_native)),
            "projectedNativeRecordSha256": sha256(encode(projected_native)),
            "discardedExistingRecordChanges": raw_inventory["changed"]["count"],
        }
        (candidate / "package-lock.json").write_bytes(projected_lock)
        candidate_lock = read_bytes(candidate / "package-lock.json", LOCK_LIMIT)
        require(candidate_lock == projected_lock, "projection_write_mismatch")
        self.success("candidate_projection", {
            "rawGenerationAccepted": False, "nativeRecordCopiedExactly": True,
            "rawLockSha256": sha256(raw_lock), "projectedLockSha256": sha256(projected_lock),
        })
        self.active_stage = "candidate_delta"
        verify_candidate(
            manifests["package.json"], manifests["package-lock.json"],
            read_bytes(candidate / "package.json", LOCK_LIMIT), candidate_lock, native,
        )
        self.immutable[candidate / "package-lock.json"] = candidate_lock
        self.success("candidate_delta", {
            "lockSha256": sha256(candidate_lock), "addedRecord": NATIVE_KEY,
            "preexistingRecordsUnchanged": True, "manifestUnchanged": True,
        })
        diff = "".join(difflib.unified_diff(
            manifests["package-lock.json"].decode().splitlines(keepends=True),
            candidate_lock.decode().splitlines(keepends=True),
            fromfile="original/package-lock.json", tofile="candidate/package-lock.json",
        )).encode()
        require(len(diff) <= REPORT_LIMIT, "candidate_diff_limit")
        (self.output / "candidate-package-lock.json").write_bytes(candidate_lock)
        (self.output / "candidate-lock.diff").write_bytes(diff)
        installed = invoke("candidate", "install", [node, str(npm), "ci", *NPM_FLAGS], 300)
        if installed.status == "succeeded":
            invoke("candidate", "guard", [node, vitest, "run", guard], 60)
            native_control("candidate")
            if self.report["stages"]["candidate_native"]["status"] == "succeeded":
                for suffix, args, seconds in (
                    ("tests", [vitest, "run"], 300),
                    ("lint", [str(Path("node_modules") / "eslint" / "bin" / "eslint.js"), "."], 180),
                    ("typecheck", [str(Path("node_modules") / "typescript" / "bin" / "tsc"), "--noEmit"], 180),
                    ("build", [str(Path("node_modules") / "next" / "dist" / "bin" / "next"), "build"], 300),
                ):
                    invoke("candidate", suffix, [node, *args], seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--collect", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--lock", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.collect is not None:
        if args.output is not None or not isinstance(args.lock, Path):
            raise DiagnosticError("invalid_worker_arguments")
        result = {}
        try:
            identity(dict(os.environ))
            require(
                args.collect.resolve().parent.parent == Path(os.environ["RUNNER_TEMP"]).resolve()
                and args.collect.name == "public"
                and args.collect.parent.name.startswith("pr477-native-")
                and args.lock.resolve()
                == args.collect.parent / "source" / "app" / "web" / "package-lock.json",
                "invalid_worker_directory",
            )
            collect_public(args.collect, object_json(read_bytes(args.lock, LOCK_LIMIT)), result)
            data = encode(result)
            require(len(data) <= REPORT_LIMIT, "public_report_limit")
            sys.stdout.buffer.write(data)
            return 0
        except (DiagnosticError, OSError) as exc:
            reason = str(exc) if isinstance(exc, DiagnosticError) else "public_worker_io_failure"
            result["error"] = reason
            data = encode(result)
            if len(data) > REPORT_LIMIT:
                data = encode({"error": "public_report_limit"})
            sys.stdout.buffer.write(data)
            return 2
    if not isinstance(args.output, Path) or args.lock is not None:
        raise DiagnosticError("output_required")
    runner_temp = Path(os.environ.get("RUNNER_TEMP", "")).resolve()
    output = args.output.resolve()
    require(
        "RUNNER_TEMP" in os.environ and output.parent == runner_temp
        and output.name == "pr477-native-evidence" and not output.exists()
        and not output.is_relative_to(ROOT),
        "runner_output_directory_required",
    )
    output.mkdir()
    diagnostic = Diagnostic(output, dict(os.environ))
    try:
        with tempfile.TemporaryDirectory(prefix="pr477-native-", dir=runner_temp) as temporary:
            returned = False
            try:
                diagnostic.run(Path(temporary))
                returned = True
            except (DiagnosticError, OSError, tarfile.TarError) as exc:
                diagnostic.failure(str(exc) if isinstance(exc, DiagnosticError) else "diagnostic_io_failure")
            finally:
                if not returned and not diagnostic.report["errors"]:
                    diagnostic.failure("unexpected_diagnostic_failure")
                try:
                    diagnostic.finish()
                except (DiagnosticError, OSError) as exc:
                    diagnostic.failure(str(exc) if isinstance(exc, DiagnosticError) else "immutability_io_failure")
    except OSError:
        diagnostic.failure("temporary_cleanup_failed")
    finally:
        for value in diagnostic.report["stages"].values():
            if value["status"] == "running":
                value.update(status="failed", reason="stage_interrupted")
        diagnostic.save()
    print(json.dumps({
        "status": diagnostic.report["status"], "candidateValidated": diagnostic.report["candidateValidated"],
        "originalValidated": diagnostic.report["originalValidated"], "adoptionAuthorized": False,
    }))
    return 0 if diagnostic.report["status"] == "both_verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
