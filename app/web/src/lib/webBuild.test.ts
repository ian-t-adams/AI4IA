// @vitest-environment jsdom
import { mkdtemp, rm, writeFile, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { webBuildId } from "../../build-id.mjs";
import {
  BUILD_CHECK_INTERVAL_MS,
  BUILD_CHECK_MAX_INTERVAL_MS,
  BUILD_CHECK_MIN_SPACING_MS,
  BUILD_CHECK_TIMEOUT_MS,
  WEB_BUILD_ENDPOINT,
  WebBuildMonitor,
  fetchDeployedBuildId,
  runningWebBuildId,
  useStaleWebBuild,
  validBuildId,
} from "./webBuild";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("build identifiers", () => {
  it("accepts only short, plain identifiers", () => {
    expect(validBuildId("3f9c2b7a1d0e4c5b6a7f")).toBe("3f9c2b7a1d0e4c5b6a7f");
    expect(validBuildId("build-a.1_2")).toBe("build-a.1_2");
    for (const value of ["", " build", "<script>", "a".repeat(129), 42, null, undefined]) {
      expect(validBuildId(value)).toBeNull();
    }
  });

  it("knows no running build unless one was inlined", () => {
    vi.stubEnv("AI4IA_WEB_BUILD_ID", "");
    expect(runningWebBuildId()).toBeNull();
    vi.stubEnv("AI4IA_WEB_BUILD_ID", "build-a");
    expect(runningWebBuildId()).toBe("build-a");
    vi.unstubAllEnvs();
  });
});

describe("fetchDeployedBuildId", () => {
  it("reads the deployed build from the uncached same-origin route", async () => {
    const fetchImpl = vi.fn(async () => json({ buildId: "build-b" }));
    await expect(fetchDeployedBuildId(undefined, fetchImpl)).resolves.toBe("build-b");
    expect(fetchImpl).toHaveBeenCalledWith(
      WEB_BUILD_ENDPOINT,
      expect.objectContaining({ cache: "no-store", credentials: "same-origin" }),
    );
    expect(WEB_BUILD_ENDPOINT.startsWith("/api/")).toBe(false);
  });

  it.each([
    ["an error status", async () => json({ buildId: "build-b" }, 503)],
    ["a network failure", async () => { throw new TypeError("Failed to fetch"); }],
    ["a body without an id", async () => json({})],
    ["a null id", async () => json({ buildId: null })],
    ["a malformed id", async () => json({ buildId: "<b>" })],
    ["a non-JSON body", async () => new Response("<html>", { status: 200 })],
  ])("treats %s as unknown, never as a new build", async (_label, implementation) => {
    await expect(fetchDeployedBuildId(undefined, vi.fn(implementation))).resolves.toBeNull();
  });
});

describe("WebBuildMonitor", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function setup(
    reads: Array<string | null | Promise<string | null>>,
    { hidden = false }: { hidden?: boolean } = {},
  ) {
    const state = { hidden };
    const fetchBuildId = vi.fn(async (signal: AbortSignal) => {
      void signal;
      const next = reads.length > 1 ? reads.shift() : reads[0];
      return next ?? null;
    });
    const onStale = vi.fn();
    const monitor = new WebBuildMonitor({
      runningBuildId: "build-a",
      fetchBuildId,
      onStale,
      isHidden: () => state.hidden,
      random: () => 0.5, // no jitter, so the schedule is exact
    });
    return { monitor, fetchBuildId, onStale, state };
  }

  it("keeps polling at the interval while the deployed build matches", async () => {
    const { monitor, fetchBuildId, onStale } = setup(["build-a"]);
    monitor.start();
    await vi.advanceTimersByTimeAsync(BUILD_CHECK_INTERVAL_MS - 1);
    expect(fetchBuildId).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(fetchBuildId).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(BUILD_CHECK_INTERVAL_MS);
    expect(fetchBuildId).toHaveBeenCalledTimes(2);
    expect(onStale).not.toHaveBeenCalled();
    monitor.stop();
  });

  it("reports a different build once and stops polling", async () => {
    const { monitor, fetchBuildId, onStale } = setup(["build-b"]);
    monitor.start();
    await vi.advanceTimersByTimeAsync(BUILD_CHECK_INTERVAL_MS);
    expect(onStale).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(BUILD_CHECK_MAX_INTERVAL_MS * 4);
    expect(fetchBuildId).toHaveBeenCalledTimes(1);
    expect(onStale).toHaveBeenCalledTimes(1);
  });

  it("treats failed reads as unknown and backs off until a matching read", async () => {
    const { monitor, fetchBuildId, onStale } = setup([null, null, "build-a"]);
    monitor.start();
    await vi.advanceTimersByTimeAsync(BUILD_CHECK_INTERVAL_MS);
    expect(fetchBuildId).toHaveBeenCalledTimes(1);
    // One failure doubles the wait.
    await vi.advanceTimersByTimeAsync(2 * BUILD_CHECK_INTERVAL_MS - 1);
    expect(fetchBuildId).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetchBuildId).toHaveBeenCalledTimes(2);
    // Two failures quadruple it.
    await vi.advanceTimersByTimeAsync(4 * BUILD_CHECK_INTERVAL_MS);
    expect(fetchBuildId).toHaveBeenCalledTimes(3);
    // A matching read resets the interval.
    await vi.advanceTimersByTimeAsync(BUILD_CHECK_INTERVAL_MS);
    expect(fetchBuildId).toHaveBeenCalledTimes(4);
    expect(onStale).not.toHaveBeenCalled();
    monitor.stop();
  });

  it("never waits longer than the ceiling however often reads fail", async () => {
    const { monitor, fetchBuildId } = setup([null]);
    monitor.start();
    await vi.advanceTimersByTimeAsync(BUILD_CHECK_INTERVAL_MS * (1 + 2 + 4));
    expect(fetchBuildId).toHaveBeenCalledTimes(3);
    // A third failure would wait eight intervals; the ceiling is shorter.
    expect(BUILD_CHECK_INTERVAL_MS * 8).toBeGreaterThan(BUILD_CHECK_MAX_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(BUILD_CHECK_MAX_INTERVAL_MS - 1);
    expect(fetchBuildId).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetchBuildId).toHaveBeenCalledTimes(4);
    await vi.advanceTimersByTimeAsync(BUILD_CHECK_MAX_INTERVAL_MS);
    expect(fetchBuildId).toHaveBeenCalledTimes(5);
    monitor.stop();
  });

  it("checks early on focus, but never more often than the spacing or in parallel", async () => {
    let finish!: (value: string | null) => void;
    const pending = new Promise<string | null>((resolve) => { finish = resolve; });
    const { monitor, fetchBuildId } = setup([pending, "build-a"]);
    monitor.start();
    monitor.nudge();
    await vi.advanceTimersByTimeAsync(BUILD_CHECK_MIN_SPACING_MS - 1);
    monitor.nudge();
    expect(fetchBuildId).not.toHaveBeenCalled();
    // Control: the identical nudge once the spacing has passed.
    await vi.advanceTimersByTimeAsync(1);
    monitor.nudge();
    expect(fetchBuildId).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(BUILD_CHECK_MIN_SPACING_MS);
    monitor.nudge(); // the spacing has passed again, but that read is in flight
    expect(fetchBuildId).toHaveBeenCalledTimes(1);
    finish("build-a");
    await vi.advanceTimersByTimeAsync(0);
    monitor.nudge(); // control: the identical nudge once the read is done
    expect(fetchBuildId).toHaveBeenCalledTimes(2);
    monitor.stop();
  });

  it("defers a due check while the tab is hidden", async () => {
    const { monitor, fetchBuildId, state } = setup(["build-a"], { hidden: true });
    monitor.start();
    await vi.advanceTimersByTimeAsync(BUILD_CHECK_INTERVAL_MS * 3);
    monitor.nudge();
    expect(fetchBuildId).not.toHaveBeenCalled();
    state.hidden = false;
    monitor.nudge();
    expect(fetchBuildId).toHaveBeenCalledTimes(1);
    monitor.stop();
  });

  it("gives up on a hung read as unknown", async () => {
    const signals: AbortSignal[] = [];
    const fetchBuildId = vi.fn(
      (signal: AbortSignal) =>
        new Promise<string | null>((resolve) => {
          signals.push(signal);
          signal.addEventListener("abort", () => resolve(null));
        }),
    );
    const onStale = vi.fn();
    const monitor = new WebBuildMonitor({
      runningBuildId: "build-a", fetchBuildId, onStale, random: () => 0.5,
    });
    monitor.start();
    await vi.advanceTimersByTimeAsync(BUILD_CHECK_INTERVAL_MS + BUILD_CHECK_TIMEOUT_MS);
    expect(signals[0]?.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(2 * BUILD_CHECK_INTERVAL_MS);
    expect(fetchBuildId).toHaveBeenCalledTimes(2);
    expect(onStale).not.toHaveBeenCalled();
    monitor.stop();
  });

  it("stops polling and ignores a late answer once stopped", async () => {
    let finish!: (value: string | null) => void;
    const pending = new Promise<string | null>((resolve) => { finish = resolve; });
    const { monitor, fetchBuildId, onStale } = setup([pending]);
    monitor.start();
    await vi.advanceTimersByTimeAsync(BUILD_CHECK_INTERVAL_MS);
    expect(fetchBuildId).toHaveBeenCalledTimes(1);
    const signal = fetchBuildId.mock.calls[0][0];
    monitor.stop();
    expect(signal.aborted).toBe(true);
    finish("build-b");
    await vi.advanceTimersByTimeAsync(BUILD_CHECK_MAX_INTERVAL_MS * 2);
    expect(onStale).not.toHaveBeenCalled();
    expect(fetchBuildId).toHaveBeenCalledTimes(1);
  });
});

describe("useStaleWebBuild", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubEnv("AI4IA_WEB_BUILD_ID", "build-a");
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  function serve(response: () => Promise<Response>) {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      void input;
      return response();
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  async function afterFirstCheck() {
    // Past the longest jittered first interval.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(Math.ceil(BUILD_CHECK_INTERVAL_MS * 1.2) + 1);
    });
  }

  it.each([
    ["the same build is deployed", async () => json({ buildId: "build-a" }), false],
    ["a different build is deployed", async () => json({ buildId: "build-b" }), true],
    ["the deployed build can't be read", async () => { throw new TypeError("offline"); }, false],
  ])("reports stale only when %s", async (_label, response, stale) => {
    const fetchMock = serve(response);
    const { result } = renderHook(() => useStaleWebBuild());
    expect(result.current).toBe(false);
    await afterFirstCheck();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toBe(WEB_BUILD_ENDPOINT);
    expect(result.current).toBe(stale);
  });

  it("checks again when the window regains focus after the spacing", async () => {
    const fetchMock = serve(async () => json({ buildId: "build-b" }));
    const { result } = renderHook(() => useStaleWebBuild());
    act(() => { window.dispatchEvent(new Event("focus")); });
    expect(fetchMock).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(BUILD_CHECK_MIN_SPACING_MS);
    });
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result.current).toBe(true);
  });

  it("never polls without an inlined build of its own", async () => {
    vi.stubEnv("AI4IA_WEB_BUILD_ID", "");
    const fetchMock = serve(async () => json({ buildId: "build-b" }));
    const { result } = renderHook(() => useStaleWebBuild());
    await afterFirstCheck();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current).toBe(false);
  });

  it("stops polling when unmounted", async () => {
    const fetchMock = serve(async () => json({ buildId: "build-a" }));
    const { unmount } = renderHook(() => useStaleWebBuild());
    unmount();
    await afterFirstCheck();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("webBuildId", () => {
  let root: string;

  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), "ai4ia-build-id-"));
    await mkdir(join(root, "src", "lib"), { recursive: true });
    await writeFile(join(root, "src", "lib", "app.ts"), "export const answer = 1;\n");
    await writeFile(join(root, "src", "lib", "app.test.ts"), "test body\n");
    await writeFile(join(root, "package.json"), "{}\n");
  });
  afterEach(async () => {
    await rm(root, { recursive: true, force: true });
  });

  it("is stable for identical sources and changes with the bundle's inputs only", async () => {
    const first = webBuildId(root);
    expect(first).toMatch(/^[0-9a-f]{20}$/);
    expect(validBuildId(first)).toBe(first);
    expect(webBuildId(root)).toBe(first);

    // A test file never reaches the bundle.
    await writeFile(join(root, "src", "lib", "app.test.ts"), "changed test body\n");
    expect(webBuildId(root)).toBe(first);

    // Control: the same kind of edit to shipped source is a new build.
    await writeFile(join(root, "src", "lib", "app.ts"), "export const answer = 2;\n");
    const second = webBuildId(root);
    expect(second).not.toBe(first);

    await writeFile(join(root, "package.json"), '{"version":"2"}\n');
    expect(webBuildId(root)).not.toBe(second);
  });
});
