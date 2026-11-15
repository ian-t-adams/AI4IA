"""Fake-gh tests for the gated dispatch wrapper. No network, no GitHub."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from scripts.tests._loader import load_script

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = load_script("dispatch_gated_workflow", ROOT / "scripts" / "dispatch_gated_workflow.py")
SHA = "c" * 40
GATE = "AI4IA_GATEWAY_RELEASE_ENABLED"
WORKFLOW = "hosted-gateway-release.yml"


class FakeGh:
    """Repository variables plus one workflow's runs; the dispatched run completes after N views."""

    def __init__(self, *, polls=3, variables=None, runs=None, new_runs=1, head=SHA,
                 fail=(), sticky_gate=False, interrupt=(), timeouts=None):
        self.variables = dict(variables or {})
        self.runs = list(runs or [{"databaseId": 1, "headSha": SHA, "event": "workflow_dispatch",
                                   "status": "completed", "createdAt": "x"}])
        self.polls, self.new_runs, self.head = polls, new_runs, head
        self.fail, self.sticky_gate, self.interrupt = set(fail), sticky_gate, set(interrupt)
        self.calls, self.gate_during_views, self.views = [], [], 0
        # key -> number of "timed out" failures, injected only once our gate has been set.
        self.timeouts = dict(timeouts or {})

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        key = " ".join(args[:2])
        if key in self.fail:
            raise WRAPPER.DispatchError(key)
        gate_was_set = any(c[:2] == ["variable", "set"] for c in self.calls[:-1])
        if gate_was_set and self.timeouts.get(key, 0) > 0:
            self.timeouts[key] -= 1
            raise WRAPPER.DispatchError(f"gh {key} timed out")
        if key in self.interrupt:
            self.interrupt.discard(key)
            raise KeyboardInterrupt(key)
        if key == "variable list":
            return json.dumps([{"name": name} for name in self.variables])
        if key == "variable set":
            self.variables[args[2]] = args[args.index("--body") + 1]
            return ""
        if key == "variable delete":
            if not self.sticky_gate:
                self.variables.pop(args[2], None)
            return ""
        if key == "workflow run":
            for offset in range(self.new_runs):
                self.runs.append({"databaseId": 43735149163 + offset, "headSha": self.head,
                                  "event": "workflow_dispatch", "status": "queued", "createdAt": "y"})
            return ""
        if key == "run list":
            return json.dumps(self.runs)
        if key == "run view":
            self.views += 1
            self.gate_during_views.append(self.variables.get(GATE))
            run_id = int(args[2])
            done = self.views >= self.polls
            return json.dumps({"databaseId": run_id, "headSha": self.head,
                               "status": "completed" if done else "in_progress",
                               "conclusion": "success" if done else ""})
        raise AssertionError(args)


def run(gh, *args, **kwargs):
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    return WRAPPER.dispatch(*(args or (WORKFLOW, SHA, "c2-web")), gh=gh, sleep=sleep,
                            clock=lambda: clock[0], log=lambda _m: None, **kwargs)


class DispatchWrapperTests(unittest.TestCase):
    def test_holds_gate_until_exact_run_is_terminal_then_verifies_absent(self):
        gh = FakeGh(polls=3)
        result = run(gh)
        self.assertEqual(result, {"workflow": WORKFLOW, "run_id": 43735149163, "status": "completed",
                                  "conclusion": "success", "gate_absent": True})
        self.assertEqual(gh.gate_during_views, ["true", "true", "true"])
        self.assertNotIn(GATE, gh.variables)
        dispatched = next(c for c in gh.calls if c[:2] == ["workflow", "run"])
        self.assertIn("stage=c2-web", dispatched)
        self.assertIn(f"reviewed_sha={SHA}", dispatched)
        self.assertEqual({c[2] for c in gh.calls if c[:2] == ["run", "view"]}, {"43735149163"})
        self.assertEqual(gh.calls[-1][:2], ["variable", "list"])

    def test_every_failure_after_setting_the_gate_deletes_and_verifies_it(self):
        cases = {
            "dispatch-fails": {"fail": {"workflow run"}},
            "no-new-run": {"new_runs": 0},
            "two-new-runs": {"new_runs": 2},
            "wrong-head": {"head": "d" * 40},
            "timeout": {"polls": 10 ** 6},
        }
        for label, kwargs in cases.items():
            gh = FakeGh(**kwargs)
            with self.subTest(label), self.assertRaises(WRAPPER.DispatchError):
                run(gh, timeout_seconds=60)
            self.assertNotIn(GATE, gh.variables)
            self.assertEqual(gh.calls[-1][:2], ["variable", "list"])

    def test_refuses_without_touching_a_gate_someone_else_holds_or_a_busy_workflow(self):
        busy = [{"databaseId": 9, "headSha": SHA, "event": "workflow_dispatch",
                 "status": "in_progress", "createdAt": "x"}]
        for label, gh in {"held": FakeGh(variables={GATE: "true"}),
                          "other-gate-held": FakeGh(variables={"AI4IA_HOSTED_WEB_READ_ENABLED": "true"}),
                          "busy": FakeGh(runs=busy)}.items():
            before = dict(gh.variables)
            with self.subTest(label), self.assertRaises(WRAPPER.DispatchError):
                run(gh)
            self.assertFalse(any(c[:2] in (["variable", "set"], ["variable", "delete"],
                                           ["workflow", "run"]) for c in gh.calls))
            self.assertEqual(gh.variables, before)

    def test_interrupts_still_end_with_every_gate_absent_by_reread(self):
        for label, keys in {"while-polling": {"run view"}, "while-dispatching": {"workflow run"},
                            "during-delete": {"run view", "variable delete"}}.items():
            gh = FakeGh(interrupt=keys)
            with self.subTest(label), self.assertRaises(KeyboardInterrupt):
                run(gh)
            self.assertIn(["variable", "set", GATE, "-R", WRAPPER.REPOSITORY, "--body", "true"], gh.calls)
            self.assertEqual(gh.variables, {})
            self.assertEqual(gh.calls[-1][:2], ["variable", "list"])

    def test_default_gh_bounds_every_call_and_maps_timeout_and_os_errors(self):
        with mock.patch.object(WRAPPER.shutil, "which", return_value="gh"):
            with (mock.patch.object(WRAPPER.subprocess, "run",
                                    side_effect=subprocess.TimeoutExpired("gh", 7)) as runner,
                  self.assertRaisesRegex(WRAPPER.DispatchError, r"^gh run view timed out$")):
                WRAPPER.default_gh(["run", "view", "1"], timeout=7)
            self.assertEqual(runner.call_args.kwargs["timeout"], 7)
            with (mock.patch.object(WRAPPER.subprocess, "run", side_effect=OSError("secret-path")),
                  self.assertRaisesRegex(WRAPPER.DispatchError, r"^gh variable delete could not be started$")):
                WRAPPER.default_gh(["variable", "delete", GATE])
            ok = subprocess.CompletedProcess(["gh"], 0, stdout="[]", stderr="")
            with mock.patch.object(WRAPPER.subprocess, "run", return_value=ok) as runner:
                WRAPPER.default_gh(["variable", "list"])
            self.assertEqual(runner.call_args.kwargs["timeout"], WRAPPER.GH_TIMEOUT_SECONDS)

    def test_timeout_while_holding_releases_gate_and_reports_bound_run(self):
        messages = []
        gh = FakeGh(polls=10, timeouts={"run view": 1})
        clock = [0.0]
        with self.assertRaisesRegex(WRAPPER.DispatchError, "timed out") as caught:
            WRAPPER.dispatch(WORKFLOW, SHA, "c2-web", gh=gh, sleep=lambda s: None,
                             clock=lambda: clock[0], log=messages.append)
        self.assertEqual(caught.exception.run_id, 43735149163)
        self.assertTrue(any("43735149163" in m and "does not stop" in m for m in messages))
        self.assertEqual(gh.variables, {})
        self.assertEqual(gh.calls[-1][:2], ["variable", "list"])

    def test_cleanup_timeouts_retry_bounded_then_verify_or_fail_unverified(self):
        gh = FakeGh(polls=1, timeouts={"variable delete": 1, "variable list": 1})
        self.assertTrue(run(gh)["gate_absent"])
        self.assertEqual(gh.variables, {})
        gh = FakeGh(polls=1, timeouts={"variable list": 10})
        with self.assertRaisesRegex(WRAPPER.DispatchError, "could not verify"):
            run(gh)
        self.assertEqual(sum(c[:2] == ["variable", "delete"] for c in gh.calls), 3)

    def test_gate_still_present_after_delete_fails_loudly(self):
        gh = FakeGh(sticky_gate=True)
        with self.assertRaisesRegex(WRAPPER.DispatchError, "STILL SET"):
            run(gh)

    def test_inputs_are_validated_before_any_gh_call(self):
        for args in (
            (WORKFLOW, "C" * 40, "c2-web"), (WORKFLOW, SHA, "c4-api"), (WORKFLOW, SHA, None),
            ("other.yml", SHA, "c2-web"), ("hosted-web-one-read.yml", SHA, "c2-web"),
            (WORKFLOW, SHA, "rollback-api"), (WORKFLOW, SHA, "c2-api", "ca-api--r1"),
        ):
            gh = FakeGh()
            with self.subTest(args=args), self.assertRaises(WRAPPER.DispatchError):
                run(gh, *args)
            self.assertEqual(gh.calls, [])

    def test_one_read_uses_its_own_gate(self):
        gh = FakeGh(polls=1)
        result = run(gh, "hosted-web-one-read.yml", SHA)
        self.assertTrue(result["gate_absent"])
        sets = [c[2] for c in gh.calls if c[:2] == ["variable", "set"]]
        self.assertEqual(sets, ["AI4IA_HOSTED_WEB_READ_ENABLED"])
        self.assertEqual(gh.variables, {})


if __name__ == "__main__":
    unittest.main()
