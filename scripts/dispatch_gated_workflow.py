"""Dispatch ONE default-off hosted workflow and hold its repository gate for exactly that run.

The hosted release/read workflows re-read their repository gate variable in every job, so
the gate must stay set until the dispatched run is terminal and must never outlive it.
This wrapper:

1. refuses if the gate is already set (someone else holds it) or a run of the workflow is
   queued/in progress;
2. snapshots existing run IDs, sets the gate to ``true`` and dispatches with ``admit=true``;
3. binds to the exact new run ID (workflow_dispatch, reviewed head SHA, not seen before;
   anything ambiguous fails closed) and polls only that ID until it is completed;
4. in ``finally`` deletes the gate and verifies it is ABSENT before returning, failing loudly
   if it is not.

Every ``gh`` call is bounded (``GH_TIMEOUT_SECONDS``); a timeout or OS error maps to a fixed
``DispatchError``. Calls and cleanup can overrun the bind/run deadlines, and failed cleanup
can leave the gate set; absence is claimed only after a successful reread. Deleting the
gate after a timeout, error or interrupt does NOT prove the dispatched run stopped: the bound run
ID is logged and reported so the operator can inspect it. There are no added retries beyond the
bounded cleanup, no auto-cancel and no new credentials.

It never touches AI4IA_DEPLOYMENT_ENABLED or any other variable, secret or setting. Uses the
caller's existing ``gh`` login; no tokens are read or printed.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence

REPOSITORY = "ian-t-adams/AI4IA"
WORKFLOWS = {
    "hosted-gateway-release.yml": "AI4IA_GATEWAY_RELEASE_ENABLED",
    "hosted-web-one-read.yml": "AI4IA_HOSTED_WEB_READ_ENABLED",
}
STAGES = ("c1-web", "c2-web", "c1-api", "c2-api", "c3-api", "rollback-api")
REVISION = re.compile(r"[a-z0-9][a-z0-9-]{0,62}--[a-z0-9-]{1,80}")


GH_TIMEOUT_SECONDS = 60.0


class DispatchError(RuntimeError):
    run_id: int | None = None


Gh = Callable[[Sequence[str]], str]


def default_gh(args: Sequence[str], *, timeout: float = GH_TIMEOUT_SECONDS) -> str:
    executable = shutil.which("gh")
    if executable is None:
        raise DispatchError("gh CLI is not installed")
    command = f"gh {args[0]} {args[1] if len(args) > 1 else ''}".strip()
    try:
        result = subprocess.run([executable, *args], capture_output=True, text=True, check=False,
                                timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DispatchError(f"{command} timed out") from None
    except OSError:
        raise DispatchError(f"{command} could not be started") from None
    if result.returncode != 0:
        raise DispatchError(f"{command} failed")
    return result.stdout


def dispatch_inputs(workflow: str, reviewed_sha: str, stage: str | None,
                    rollback_revision: str | None) -> list[str]:
    if workflow not in WORKFLOWS:
        raise DispatchError("workflow is not an allowlisted gated workflow")
    if not re.fullmatch(r"[0-9a-f]{40}", reviewed_sha):
        raise DispatchError("reviewed SHA must be 40 lowercase hex characters")
    fields = ["-f", "admit=true", "-f", f"reviewed_sha={reviewed_sha}"]
    if workflow == "hosted-gateway-release.yml":
        if stage not in STAGES:
            raise DispatchError("gateway release requires exactly one known stage")
        fields += ["-f", f"stage={stage}"]
        if stage == "rollback-api":
            if not rollback_revision or not REVISION.fullmatch(rollback_revision):
                raise DispatchError("rollback-api requires one named revision")
            fields += ["-f", f"rollback_revision={rollback_revision}"]
        elif rollback_revision:
            raise DispatchError("rollback revision is accepted only for rollback-api")
    elif stage or rollback_revision:
        raise DispatchError("the one-read workflow accepts no stage inputs")
    return fields


def present_gates(gh: Gh, repo: str) -> set[str]:
    names = {item.get("name") for item in json.loads(
        gh(["variable", "list", "-R", repo, "--json", "name"]) or "[]")}
    return names & set(WORKFLOWS.values())


def release_gate(gh: Gh, repo: str, gate: str, log: Callable[[str], None]) -> None:
    """Delete our gate (retrying, even across interrupts) and verify EVERY gate is absent by reread."""
    interrupted: BaseException | None = None
    verified = False
    for _attempt in range(3):
        try:
            gh(["variable", "delete", gate, "-R", repo])
        except DispatchError:
            pass  # an already-absent gate is fine; the reread below decides.
        except BaseException as error:  # noqa: BLE001 - finish cleanup, then re-raise the interrupt.
            interrupted = interrupted or error
        try:
            remaining = present_gates(gh, repo)
        except DispatchError:
            continue  # unverified (e.g. timed out); retry the bounded delete + reread.
        except BaseException as error:  # noqa: BLE001
            interrupted = interrupted or error
            continue
        verified = True
        if not remaining:
            log("all gate variables verified ABSENT")
            if interrupted is not None:
                raise interrupted
            return
        if gate not in remaining:
            raise DispatchError(f"gate(s) {sorted(remaining)} are set by another holder; clear manually")
    if not verified:
        raise DispatchError(f"could not verify {gate} is absent (gh failed); check and delete it manually now")
    raise DispatchError(f"{gate} is STILL SET after the run; delete it manually now")


def runs(gh: Gh, repo: str, workflow: str) -> list[dict]:
    return json.loads(gh([
        "run", "list", "-R", repo, "-w", workflow, "--limit", "50",
        "--json", "databaseId,headSha,event,status,createdAt",
    ]) or "[]")


def dispatch(workflow: str, reviewed_sha: str, stage: str | None = None,
             rollback_revision: str | None = None, *, repo: str = REPOSITORY, gh: Gh = default_gh,
             sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
             bind_seconds: float = 120, timeout_seconds: float = 3600, poll_seconds: float = 15,
             log: Callable[[str], None] = lambda message: print(message, file=sys.stderr)) -> dict:
    fields = dispatch_inputs(workflow, reviewed_sha, stage, rollback_revision)
    gate = WORKFLOWS[workflow]
    held = present_gates(gh, repo)
    if held:
        raise DispatchError(f"{sorted(held)} already set; refusing to share or clear another holder's gate")
    existing = runs(gh, repo, workflow)
    if any(run.get("status") != "completed" for run in existing):
        raise DispatchError("a run of this workflow is still queued or in progress")
    seen = {run["databaseId"] for run in existing}
    result: dict = {"workflow": workflow, "run_id": None, "status": None, "conclusion": None}
    try:
        _hold_gate_for_one_run(gh, repo, workflow, gate, fields, reviewed_sha, seen, result,
                               sleep, clock, bind_seconds, timeout_seconds, poll_seconds, log)
    except BaseException as error:
        if result["run_id"] is not None:
            log(f"stopped holding bound run {result['run_id']}; gate deletion does not stop it - "
                "inspect that run")
            if isinstance(error, DispatchError):
                error.run_id = result["run_id"]
        raise
    finally:
        release_gate(gh, repo, gate, log)
        result["gate_absent"] = True
    return result


def _hold_gate_for_one_run(gh: Gh, repo: str, workflow: str, gate: str, fields: list[str],
                           reviewed_sha: str, seen: set, result: dict, sleep, clock,
                           bind_seconds: float, timeout_seconds: float, poll_seconds: float,
                           log: Callable[[str], None]) -> None:
    gh(["variable", "set", gate, "-R", repo, "--body", "true"])
    log(f"{gate} set for one dispatch")
    gh(["workflow", "run", workflow, "-R", repo, "--ref", "main", *fields])
    deadline = clock() + bind_seconds
    while True:
        new = [run for run in runs(gh, repo, workflow)
               if run["databaseId"] not in seen and run.get("event") == "workflow_dispatch"]
        if len(new) > 1:
            raise DispatchError("more than one new run appeared; refusing to guess which to hold")
        if new:
            if new[0].get("headSha") != reviewed_sha:
                raise DispatchError("the new run is not at the reviewed SHA")
            run_id = new[0]["databaseId"]
            break
        if clock() >= deadline:
            raise DispatchError("no new run appeared within the bind window")
        sleep(5)
    result["run_id"] = run_id
    log(f"bound to run {run_id}")
    deadline = clock() + timeout_seconds
    while True:
        view = json.loads(gh(["run", "view", str(run_id), "-R", repo,
                              "--json", "databaseId,status,conclusion,headSha"]))
        if view.get("databaseId") != run_id or view.get("headSha") != reviewed_sha:
            raise DispatchError("run view did not return the bound run")
        result["status"] = view.get("status")
        if view.get("status") == "completed":
            result["conclusion"] = view.get("conclusion")
            break
        if clock() >= deadline:
            raise DispatchError("bound run did not complete before the timeout")
        sleep(poll_seconds)


def _sigterm(_signum, _frame):
    raise KeyboardInterrupt("SIGTERM")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workflow", required=True, choices=sorted(WORKFLOWS))
    parser.add_argument("--reviewed-sha", required=True)
    parser.add_argument("--stage", choices=STAGES)
    parser.add_argument("--rollback-revision")
    parser.add_argument("--repo", default=REPOSITORY)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--gh-timeout", type=float, default=GH_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    if not 0 < args.gh_timeout <= 300:
        parser.error("--gh-timeout must be in (0, 300] seconds")
    signal.signal(signal.SIGTERM, _sigterm)
    try:
        result = dispatch(args.workflow, args.reviewed_sha, args.stage, args.rollback_revision,
                          repo=args.repo, timeout_seconds=args.timeout,
                          gh=lambda gh_args: default_gh(gh_args, timeout=args.gh_timeout))
    except DispatchError as error:
        bound = f" (bound run {error.run_id} may still be running)" if error.run_id else ""
        print(f"refused: {error}{bound}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["conclusion"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
