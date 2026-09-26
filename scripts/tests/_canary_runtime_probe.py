"""One synthetic canary lifecycle through the real CLI, in the declared runtime only.

``test_canary_runtime`` runs this in a fresh virtual environment that holds only
what ``application-canaries.yml`` installs. Only the network is replaced: GitHub
REST, the OIDC/Entra exchange and the application. The committed catalog and
price book, every lazy import and the state files are real, so a dependency the
workflow does not install fails here instead of in a live observation.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import sysconfig
import tempfile
from collections import Counter
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import aiohttp

from scripts.canaries import __main__ as cli
from scripts.canaries import monitor
from scripts.canaries.configuration import Configuration, RealtimeActor
from scripts.canaries.contracts import (
    INTERVAL_SECONDS, MAX_OUTPUT_TOKENS, WORKFLOW, Run, stamp, utc_now,
)
from scripts.canaries.github import artifact_name
from scripts.canaries.state import State
from scripts.canaries.transport import Response

ROOT = Path(__file__).resolve().parents[2]
MARK = "CANARY-RUNTIME-PROBE "
REPOSITORY, REPOSITORY_ID, WORKFLOW_ID, REGION = "owner/repository", 700, 123, "eastus2"
FIRST = Run(REPOSITORY, REPOSITORY_ID, 100, 1, 1, "a" * 40)
SECOND = Run(REPOSITORY, REPOSITORY_ID, 200, 2, 1, "b" * 40)
MONITOR_TOKEN, REALTIME_TOKEN, REPOSITORY_TOKEN = "probe-monitor", "probe-realtime", "probe-repository"
REALTIME_CLIENT = "88888888-8888-8888-8888-888888888888"
CLEANUP = json.loads((ROOT / "scripts" / "fixtures" / "conversation-cleanup.json").read_text(encoding="utf-8"))
SESSION = CLEANUP["session"]["id"]
SOURCE = json.loads((ROOT / "infra" / "models.json").read_text(encoding="utf-8"))
CONFIG = Configuration(
    tenant_id="11111111-1111-1111-1111-111111111111",
    client_id="22222222-2222-2222-2222-222222222222",
    object_id="33333333-3333-3333-3333-333333333333",
    audience="api://44444444-4444-4444-4444-444444444444",
    web_origin="https://web.example.test",
    api_origin="https://api.example.test",
    approval_id="55555555-5555-5555-5555-555555555555",
    expires_at=stamp(utc_now() + timedelta(days=1)),
    approved_runs=4,
    interval_seconds=INTERVAL_SECONDS,
    acknowledge_no_hard_bill_cap=True,
    actor_ready=True,
    cleanup_approved=True,
    ga_enabled=True,
    realtime_actor=RealtimeActor(REALTIME_CLIENT, "99999999-9999-9999-9999-999999999999"),
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def reply(value: Any, status: int = 200) -> Response:
    return Response(status, json.dumps(value).encode("utf-8"), 0.01, "application/json")


def deployment(model: str) -> str:
    return f"{model}-probe"


def environment(run: Run, runner: Path, operation: str) -> dict[str, str]:
    runner.mkdir(parents=True, exist_ok=True)
    return {
        "GITHUB_REPOSITORY": run.repository, "GITHUB_REPOSITORY_ID": str(run.repository_id),
        "GITHUB_RUN_ID": str(run.run_id), "GITHUB_RUN_NUMBER": str(run.number),
        "GITHUB_RUN_ATTEMPT": str(run.attempt), "GITHUB_SHA": run.sha,
        "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_WORKFLOW_REF": f"{run.repository}/{WORKFLOW}@refs/heads/main",
        "GITHUB_SERVER_URL": "https://github.com", "GITHUB_API_URL": "https://api.github.com",
        "GITHUB_OUTPUT": str(runner / "outputs"), "GITHUB_STEP_SUMMARY": str(runner / "summary"),
        "GH_TOKEN": REPOSITORY_TOKEN, "CANARY_OPERATION": operation,
        "AI4IA_CANARY_ENABLED": "true", "AI4IA_CANARY_CONFIG": json.dumps(asdict(CONFIG)),
        "AI4IA_CANARY_HARD_USD_CAP": "", "DEPLOY_CLIENT_ID": "66666666-6666-6666-6666-666666666666",
    }


def metadata(run: Run, created: str, updated: str, status: str) -> dict[str, Any]:
    return {
        "id": run.run_id, "run_number": run.number, "run_attempt": run.attempt,
        "head_sha": run.sha, "workflow_id": WORKFLOW_ID, "head_branch": "main",
        "repository": {"id": run.repository_id, "full_name": run.repository},
        "head_repository": {"id": run.repository_id}, "event": "workflow_dispatch",
        "status": status, "created_at": created, "updated_at": updated,
    }


class GitHub:
    """The four exact read-only REST reads `locate` makes."""

    def __init__(self, current: dict[str, Any], previous: dict[str, Any] | None = None, size: int = 0):
        self.current, self.previous, self.size = current, previous, size

    async def __aenter__(self) -> GitHub:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def request(self, method: str, url: str, *, token: str | None = None, **_: Any) -> Response:
        require(method == "GET" and token == REPOSITORY_TOKEN, f"unexpected GitHub call {method}")
        root = f"/repos/{REPOSITORY}/actions"
        path = urlsplit(url).path
        if path == f"{root}/workflows/{WORKFLOW.rsplit('/', 1)[-1]}":
            return reply({"id": WORKFLOW_ID, "path": WORKFLOW})
        if path == f"{root}/runs/{self.current['id']}":
            return reply(self.current)
        if self.previous is not None and path == f"{root}/workflows/{WORKFLOW_ID}/runs":
            return reply({"workflow_runs": [self.current, self.previous]})
        if self.previous is not None and path == f"{root}/runs/{FIRST.run_id}/artifacts":
            return reply({"total_count": 1, "artifacts": [{
                "id": 99, "name": artifact_name(FIRST), "expired": False, "size_in_bytes": self.size,
                "workflow_run": {
                    "id": FIRST.run_id, "repository_id": REPOSITORY_ID,
                    "head_repository_id": REPOSITORY_ID, "head_branch": "main", "head_sha": FIRST.sha,
                },
            }]})
        raise AssertionError(f"unexpected GitHub path {path}")


class Socket:
    protocol = "ai4ia-bearer"

    def __init__(self, application: Application) -> None:
        self.application = application

    async def __aenter__(self) -> Socket:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def send_str(self, value: str) -> None:
        self.application.frames.append(json.loads(value))

    async def close(self, **_: Any) -> None:
        return None

    def __aiter__(self) -> Any:
        async def events() -> Any:
            for kind in ("session.created", "session.updated"):
                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps({"type": kind}))
        return events()


class Application:
    """Accepts exactly the governed request shapes; the monitor verifies the rest."""

    handshake_protocol = "ga"

    def __init__(self) -> None:
        self.calls: Counter[tuple[str, str]] = Counter()
        self.frames: list[Any] = []
        self.sockets = 0
        self.message: dict[str, Any] | None = None
        self.chat_rows = [
            {
                "id": row["name"], "category": row["category"], "api": row.get("api", "chat"),
                "conversational": True, "supportsSampling": True, "reasoningEffortOptions": [],
                "maxOutputTokens": 1024,
                "options": [{"deploymentName": deployment(row["name"]), "region": REGION}],
            }
            for row in SOURCE["catalog"]
            if row.get("category") in ("chat", "chat-fast") and row.get("deployments")
            and row.get("api", "chat") in ("chat", "responses") and row.get("runtimeEnabled", True) is True
        ]
        realtime = sorted(
            row["name"] for row in SOURCE["catalog"]
            if row.get("category") == "realtime" and row.get("deployments")
            and row.get("runtimeEnabled", True) is True
        )
        self.realtime_row = {"id": realtime[0], "category": "realtime", "options": [
            {"deploymentName": deployment(realtime[0]), "region": REGION},
        ]}

    async def __aenter__(self) -> Application:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def selection(self) -> tuple[Any, Any]:
        # The monitor has already loaded this cached book before its first use here.
        book = monitor.load_model_pricing()
        return monitor.select_chat(SOURCE, {"models": self.chat_rows}, book), book

    def assistant(self, request: dict[str, Any]) -> dict[str, Any]:
        selected, book = self.selection()
        require(
            request.get("model") == selected.model and request.get("allowTools") is False
            and request.get("allowAutomaticMemory") is False and request.get("requireFreshSession") is True,
            "the chat request was not the governed sentinel shape",
        )
        cost = book.snapshot_token_prices(selected.model).estimate(
            selected.model, prompt_tokens=12, completion_tokens=1,
        )
        call = {
            "modelId": selected.model, "api": selected.api, "coverage": "recorded",
            "providerCompleted": True, "parameters": {"maxOutputTokens": MAX_OUTPUT_TOKENS},
            "httpAttempts": 1, "usageKnown": True, "usageComplete": True,
            "promptTokens": 12, "completionTokens": 1,
            "cost": {
                "coverage": "known", "currency": "USD", "priceVersion": cost.version,
                "priceInputPer1M": cost.input_per_1m, "priceOutputPer1M": cost.output_per_1m,
                "estCostMicroUsd": cost.micro_usd,
            },
        }
        return {
            "id": "d" * 32, "sessionId": SESSION, "role": "assistant", "status": "complete",
            "model": deployment(selected.model), "content": "ready", "agent": None,
            "attachments": [], "pendingApprovals": None,
            "executionReceipt": {
                "version": 1, "status": "complete", "partial": False, "truncated": False,
                "toolCallCount": 0, "toolsOfferedCount": 0, "toolCalls": [], "toolsOffered": [],
                "delegations": [], "iterations": 1,
                "runtime": {
                    "modelId": selected.model, "api": selected.api, "modelCallCount": 1,
                    "modelCalls": [call],
                },
            },
        }

    def client(self, url: str, *, websocket: bool = False) -> Application:
        require(websocket and url.startswith("wss://api.example.test/api/voice/live?"), url)
        return self

    def ws_connect(self, _url: str, **options: Any) -> Socket:
        require(options.get("protocols") == ("ai4ia-bearer", REALTIME_TOKEN), "wrong realtime token")
        self.sockets += 1
        return Socket(self)

    async def request(
        self, method: str, url: str, *, token: str | None = None, body: bytes | None = None, **_: Any,
    ) -> Response:
        parsed = urlsplit(url)
        origin, path = f"{parsed.scheme}://{parsed.netloc}", parsed.path
        self.calls[method, path] += 1
        constraints = {
            "allowTools": False, "allowAutomaticMemory": False, "requireFreshSession": True,
            "maxOutputTokens": MAX_OUTPUT_TOKENS, "libraryDocumentIds": [],
        }
        if origin == CONFIG.api_origin:
            if (method, path) == ("GET", "/api/voice/live/config"):
                return reply({"openaiRealtimeProtocol": "ga", "enabledProviderIds": ["azure_openai"]})
            require(token == REALTIME_TOKEN, f"realtime read without its own token: {path}")
            if (method, path) == ("GET", "/api/models"):
                return reply({"models": [self.realtime_row]})
            if (method, path) == ("GET", "/api/canary/realtime-capabilities"):
                return reply({
                    "version": 1, "ready": True, "model": self.realtime_row["id"], "region": REGION,
                    "constraints": {
                        "provider": "azure_openai", "protocol": "ga", "setupOnly": True,
                        "allowAudio": False, "allowResponses": False, "allowTools": False,
                        "maxSeconds": 15,
                    },
                })
            raise AssertionError(f"unexpected API call {method} {path}")
        require(origin == CONFIG.web_origin and token == MONITOR_TOKEN, f"unexpected call {origin}")
        sessions = f"/api/sessions/{SESSION}"
        if (method, path) == ("GET", "/api/models"):
            return reply({"models": self.chat_rows})
        if (method, path) == ("GET", "/api/canary/capabilities"):
            require(parse_qs(parsed.query) == {"selection": ["least_estimated_cost"]}, parsed.query)
            selected, _ = self.selection()
            return reply({
                "version": 1, "ready": True, "model": selected.model, "api": selected.api,
                "region": REGION, "constraints": constraints,
            })
        if (method, path) == ("POST", "/api/sessions"):
            model = json.loads(body or b"{}").get("model")
            return reply({
                "id": SESSION, "model": model, "agentName": None, "libraryDocumentIds": [],
                "toolOverrides": {"added": [], "removed": []},
            }, 201)
        if (method, path) == ("POST", "/api/chat"):
            self.message = self.assistant(json.loads(body or b"{}"))
            return reply({"sessionId": SESSION, "message": self.message})
        if (method, path) == ("GET", f"{sessions}/messages"):
            return reply([{"role": "user", "content": "synthetic"}, self.message])
        if (method, path) == ("DELETE", sessions):
            return reply(CLEANUP["pending"], 202)
        if (method, path) in (("POST", f"{sessions}/deletion/reconcile"), ("GET", f"{sessions}/deletion")):
            return reply(CLEANUP["verified"])
        raise AssertionError(f"unexpected web call {method} {path}")


async def acquire(config: Configuration, *_: Any, **__: Any) -> str:
    return REALTIME_TOKEN if config.client_id == REALTIME_CLIENT else MONITOR_TOKEN


def invoke(command: str, directory: Path, env: dict[str, str]) -> None:
    # The workflow's `python -m scripts.canaries <command> --directory ...` entry point.
    with patch.dict(os.environ, env):
        code = cli.main([command, "--directory", str(directory)])
    require(code == 0, f"{command} exited {code}")


def lifecycle(scratch: Path) -> tuple[State, Application]:
    first, second, observed = scratch / "first", scratch / "second", scratch / "observed"
    started = stamp(utc_now() - timedelta(minutes=1))
    env = environment(FIRST, scratch / "first-runner", "bootstrap")
    with patch("scripts.canaries.transport.Transport", return_value=GitHub(
        metadata(FIRST, started, started, "in_progress"),
    )):
        invoke("locate", first, env)
    invoke("prepare", first, env)
    invoke("notify", first, env)
    retained = (first / "state.json").read_bytes()
    require(State.parse(json.loads(retained)).control == "bootstrap", "bootstrap was not admitted")

    finished = stamp(utc_now())
    env = environment(SECOND, scratch / "second-runner", "observe")
    with patch("scripts.canaries.transport.Transport", return_value=GitHub(
        metadata(SECOND, finished, finished, "in_progress"),
        metadata(FIRST, started, finished, "completed"), len(retained),
    )):
        invoke("locate", second, env)
    # actions/download-artifact places exactly the predecessor's state file here.
    (second / "previous").mkdir()
    (second / "previous" / "state.json").write_bytes(retained)
    invoke("prepare", second, env)
    require("observe=true" in (scratch / "second-runner" / "outputs").read_text(encoding="utf-8"),
            "observation was not admitted")

    # The observation job starts from a fresh runner holding only the handoff.
    observed.mkdir()
    shutil.copyfile(second / "handoff.json", observed / "handoff.json")
    application = Application()
    with (
        patch("scripts.canaries.identity.acquire", new=acquire),
        patch("scripts.canaries.transport.Transport", return_value=application),
    ):
        invoke("observe", observed, env)
    invoke("notify", observed, env)
    return State.parse(json.loads((observed / "state.json").read_bytes())), application


def origins() -> tuple[list[str], list[str]]:
    """Third-party modules loaded from this environment, and anything loaded from elsewhere."""
    paths = sysconfig.get_paths()
    site = {Path(paths[key]).resolve() for key in ("purelib", "platlib")}
    source = {(ROOT / "scripts").resolve(), (ROOT / "app" / "api" / "src").resolve()}
    base = {Path(sys.base_prefix).resolve(), Path(sys.base_exec_prefix).resolve()}
    third_party: set[str] = set()
    outside: list[str] = []
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if not file:
            continue
        path = Path(file).resolve()
        if any(path.is_relative_to(root) for root in site):
            third_party.add(name.partition(".")[0])
        elif not any(path.is_relative_to(root) for root in source) and not (
            any(path.is_relative_to(root) for root in base) and "site-packages" not in path.parts
        ):
            outside.append(name)
    return sorted(third_party), sorted(outside)


def main() -> int:
    def offline(*_: Any, **__: Any) -> Any:
        raise OSError("The runtime probe must not resolve a network name.")

    with tempfile.TemporaryDirectory() as temporary, patch("socket.getaddrinfo", offline):
        final, application = lifecycle(Path(temporary))
    third_party, outside = origins()
    report = final.report
    summary = {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "coverage": report.coverage,
        "stages": {name: [row.outcome, row.code] for name, row in report.stages.items()},
        "observations": final.observations, "blocked": final.blocked, "control": final.control,
        "chat": asdict(final.chat), "realtime": asdict(final.realtime),
        "attempts": [report.chat_attempts, report.realtime_attempts],
        "usage_known": report.usage_known,
        "price_version_matches": report.price_version == monitor.load_model_pricing().version,
        "writes": sorted(f"{method} {path}" for (method, path), count in application.calls.items()
                         for _ in range(count) if method != "GET"),
        "sockets": application.sockets,
        "setup_frames": application.frames == [json.loads(monitor.SETUP_INPUT)],
        "api_modules": sorted(
            name for name in sys.modules
            if name.partition(".")[0] == "ai4ia_api" or name.startswith("app.api.src.ai4ia_api")
        ),
        "third_party": third_party,
        "outside": outside,
    }
    print(MARK + json.dumps(summary, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
