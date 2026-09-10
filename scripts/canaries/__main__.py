"""Workflow CLI. No command discovers credentials, creates identities, or deploys."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from .configuration import Configuration, current_run
from .contracts import (
    CanaryError, MAX_HTTP_BYTES, MAX_REPORT_BYTES, Report, Run,
    encoded, obj, stamp, strict_json, timestamp, utc_now,
)
from .state import State, admit, finish

ROOT = Path(__file__).resolve().parents[2]


def read_json(path: Path, *, limit: int = MAX_REPORT_BYTES) -> Any:
    try:
        if path.is_symlink():
            raise CanaryError("state_invalid")
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        return strict_json(raw, limit=limit)
    except OSError as exc:
        raise CanaryError("state_missing") from exc


def write_json(path: Path, value: Any) -> None:
    raw = encoded(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("xb") as stream:
        stream.write(raw)
    temporary.replace(path)


def output(env: Mapping[str, str], key: str, value: str) -> None:
    path = env.get("GITHUB_OUTPUT")
    if path:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(f"{key}={value}\n")


def write_state(directory: Path, state: State) -> None:
    write_json(directory / "state.json", state.document())


async def locate_command(directory: Path, env: Mapping[str, str]) -> int:
    from .github import locate
    from .transport import Transport

    run = current_run(env)
    try:
        async with Transport({"https://api.github.com"}) as transport:
            predecessor = await locate(run, env.get("GH_TOKEN", ""), transport=transport)
        write_json(directory / "history.json", {
            "run": run.__dict__, "code": "ok",
            "predecessor": predecessor.document() if predecessor else None,
        })
        output(env, "artifact_id", str(predecessor.artifact_id) if predecessor else "")
        output(env, "previous_run_id", str(predecessor.run.run_id) if predecessor else "")
    except CanaryError as exc:
        write_json(directory / "history.json", {
            "run": run.__dict__, "code": exc.code, "predecessor": None,
        })
        output(env, "artifact_id", "")
        output(env, "previous_run_id", "")
    return 0


def _previous(directory: Path, run: Run) -> tuple[State | None, Any]:
    from .github import Predecessor

    history = obj(read_json(directory / "history.json"), {"run", "code", "predecessor"})
    if Run.parse(history["run"]) != run or history["code"] != "ok":
        raise CanaryError("state_missing")
    if history["predecessor"] is None:
        if run.number != 1:
            raise CanaryError("state_missing")
        return None, None
    metadata = Predecessor.parse(history["predecessor"])
    previous_dir = directory / "previous"
    if not previous_dir.is_dir() or sorted(path.name for path in previous_dir.iterdir()) != ["state.json"]:
        raise CanaryError("state_missing")
    previous = State.parse(read_json(previous_dir / "state.json"))
    metadata.validate_state(previous, run, utc_now())
    return previous, metadata


def prepare_command(directory: Path, env: Mapping[str, str]) -> int:
    run = current_run(env)
    now = utc_now()
    report = Report(run, stamp(now))
    previous: State | None = None
    config: Configuration | None = None
    try:
        previous, metadata = _previous(directory, run)
        config = Configuration.load(env, now)
        operation = env.get("CANARY_OPERATION", "observe")
        if operation not in ("observe", "bootstrap") or (
            operation == "bootstrap" and env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        ):
            raise CanaryError("invalid_configuration")
        if config is None:
            report.unobserved("disabled")
            state = finish(report, None, previous, control="disabled")
        else:
            admit(config, run, previous, now, bootstrap=operation == "bootstrap")
            if operation == "observe":
                write_json(directory / "handoff.json", {
                    "run": run.__dict__,
                    "prepared_at": stamp(now),
                    "scope_digest": config.scope_digest,
                    "previous": previous.document() if previous else None,
                    "predecessor": metadata.document() if metadata else None,
                })
                output(env, "observe", "true")
                return 0
            report.unobserved("bootstrap")
            state = finish(report, config, previous, control="bootstrap")
    except CanaryError as exc:
        report.unobserved(exc.code)
        # Losing a predecessor can hide an accepted create/dispatch. This is a
        # durable stop, not an implicit baseline or a retry on the next tick.
        report.cleanup_safe = (
            False if exc.code in {"state_missing", "state_invalid", "state_stale", "state_gap"}
            else previous.report.cleanup_safe if previous else run.number == 1
        )
        state = finish(report, config, previous, control="blocked")
    write_state(directory, state)
    output(env, "observe", "false")
    return 0


async def observe_command(directory: Path, env: Mapping[str, str]) -> int:
    from .github import Predecessor
    from .identity import acquire
    from .monitor import Budget, chat, realtime
    from .transport import Transport

    run = current_run(env)
    now = utc_now()
    report = Report(run, stamp(now))
    config: Configuration | None = None
    previous: State | None = None
    attempted = False
    control = "blocked"
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    installed_signal = False
    if sys.platform != "win32" and task is not None:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        installed_signal = True
    try:
        handoff = obj(read_json(directory / "handoff.json"), {
            "run", "prepared_at", "scope_digest", "previous", "predecessor",
        })
        age = now - timestamp(handoff["prepared_at"])
        if (
            Run.parse(handoff["run"]) != run or not timedelta(0) <= age <= timedelta(minutes=10)
        ):
            raise CanaryError("state_invalid")
        if handoff["previous"] is not None:
            candidate = State.parse(handoff["previous"])
            Predecessor.parse(handoff["predecessor"]).validate_state(candidate, run, now)
            previous = candidate
        config = Configuration.load(env, now)
        if config is None:
            raise CanaryError("disabled")
        if handoff["scope_digest"] != config.scope_digest:
            raise CanaryError("state_invalid")
        admit(config, run, previous, now, bootstrap=False)
        source = obj(read_json(ROOT / "infra" / "models.json", limit=MAX_HTTP_BYTES))
        attempted = True
        control = "observe"
        budget = Budget.start(config)
        async with asyncio.timeout_at(budget.ends_at):
            token = await acquire(config, run, env, now)
            # The access token stays only in this process. Mask commands are
            # consumed by the runner, never included in retained reports.
            if env.get("GITHUB_ACTIONS") == "true":
                print(f"::add-mask::{token}", flush=True)
            async with Transport(
                {config.web_origin, config.api_origin},
                correlation_id=f"application-canary-{run.run_id}-1",
            ) as transport:
                advertised = await chat(transport, config, token, source, report, budget=budget)
                await realtime(transport, config, token, source, advertised, report, budget=budget)
    except CanaryError as exc:
        if attempted:
            report.mark("auth", "fail", exc.code, attempts=1)
        else:
            report.unobserved(exc.code)
            # A failed handoff read cannot erase the lease or an earlier
            # unresolved write. No new baseline may be inferred from it.
            report.cleanup_safe = bool(previous and previous.report.cleanup_safe)
            if exc.code in {"state_missing", "state_invalid", "state_stale", "state_gap"}:
                report.cleanup_safe = False
    except (TimeoutError, asyncio.CancelledError) as exc:
        report.mark(
            "realtime" if report.realtime_attempts else "gateway",
            "unknown", "deadline" if isinstance(exc, TimeoutError) else "cancelled",
        )
        if report.chat_attempts and report.stages["cleanup"].outcome not in ("pass", "partial"):
            report.cleanup_safe = False
        control = "blocked"
    finally:
        if installed_signal:
            loop.remove_signal_handler(signal.SIGTERM)
    measured = [stage.outcome for stage in report.stages.values()]
    report.coverage = (
        "complete" if all(value == "pass" for value in measured)
        else "partial" if any(value in ("pass", "fail", "partial") for value in measured)
        else "unscored"
    )
    write_state(directory, finish(report, config, previous, control=control, attempted=attempted))
    return 0


def notify_command(directory: Path, env: Mapping[str, str]) -> int:
    state = State.parse(read_json(directory / "state.json"))
    if state.report.run != current_run(env):
        raise CanaryError("state_invalid")
    summary = state.report.markdown()
    for name, counter in (("chat", state.chat), ("realtime", state.realtime)):
        count = str(counter.failures) if counter.failures is not None else "unknown"
        summary += f"\n{name}: consecutive failures **{count}**, alerting **{str(counter.alerting).lower()}**.\n"
        if counter.transition == "firing":
            print(f"::error::Application canary {name}: consecutive-failure threshold reached.")
        elif counter.transition == "recovered":
            print(f"::notice::Application canary {name}: observed recovery.")
        elif counter.alerting:
            print(f"::warning::Application canary {name}: alert remains active; no recovery is claimed.")
    if state.blocked:
        print("::error::Application canary state is blocked. No automatic retry, baseline reset, or cleanup scan is authorized.")
    elif state.report.coverage != "complete":
        print("::warning::Application canary coverage is partial or unscored; collection success is not application health.")
    target = env.get("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as stream:
            stream.write(summary)
    else:
        print(summary)
    return 3 if state.blocked or state.control == "blocked" or any(
        counter.transition == "firing" for counter in (state.chat, state.realtime)
    ) else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("locate", "prepare", "observe", "notify"))
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        current_run(os.environ)
        if args.command == "locate":
            return asyncio.run(locate_command(args.directory, os.environ))
        if args.command == "prepare":
            return prepare_command(args.directory, os.environ)
        if args.command == "observe":
            return asyncio.run(observe_command(args.directory, os.environ))
        return notify_command(args.directory, os.environ)
    except (CanaryError, OSError) as exc:
        code = exc.code if isinstance(exc, CanaryError) else "collection_failed"
        print(f"::error::Application canary could not publish valid evidence: {code}.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
