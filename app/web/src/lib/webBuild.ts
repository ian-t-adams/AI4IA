"use client";

// Stale-build detection for a long-lived tab.
//
// The tab's own build identifier is inlined at build time (next.config.mjs);
// the deployed one is read from the Next app's public `/build-id` route. When
// they differ, a newer build is serving and this tab is running old client
// code, including the Voice Live client. The tab only ever offers a reload: it
// never reloads by itself. A failed or malformed read is unknown, never stale.
import { useEffect, useState } from "react";

export const WEB_BUILD_ENDPOINT = "/build-id";
export const BUILD_CHECK_INTERVAL_MS = 5 * 60_000;
export const BUILD_CHECK_MAX_INTERVAL_MS = 30 * 60_000;
// Focus and visibility changes check at most this often.
export const BUILD_CHECK_MIN_SPACING_MS = 60_000;
export const BUILD_CHECK_TIMEOUT_MS = 10_000;

const BUILD_ID_PATTERN = /^[A-Za-z0-9._-]{1,128}$/;

export function validBuildId(value: unknown): string | null {
  return typeof value === "string" && BUILD_ID_PATTERN.test(value) ? value : null;
}

/** The build this tab is running, or null when none was inlined (dev, tests). */
export function runningWebBuildId(): string | null {
  return validBuildId(process.env.AI4IA_WEB_BUILD_ID);
}

/** The deployed build, or null when it can't be read. */
export async function fetchDeployedBuildId(
  signal?: AbortSignal,
  fetchImpl: typeof fetch = fetch,
): Promise<string | null> {
  try {
    const response = await fetchImpl(WEB_BUILD_ENDPOINT, {
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal,
    });
    if (!response.ok) return null;
    const body: unknown = await response.json();
    return validBuildId(
      typeof body === "object" && body !== null ? (body as { buildId?: unknown }).buildId : null,
    );
  } catch {
    return null;
  }
}

/** The single place the app reloads itself, and only on a user's request. */
export function reloadPage(): void {
  window.location.reload();
}

export interface WebBuildMonitorOptions {
  runningBuildId: string;
  fetchBuildId: (signal: AbortSignal) => Promise<string | null>;
  onStale: () => void;
  isHidden?: () => boolean;
  now?: () => number;
  random?: () => number;
  intervalMs?: number;
  maxIntervalMs?: number;
  minSpacingMs?: number;
  timeoutMs?: number;
}

/**
 * Polls the deployed build on a jittered interval, backs off on failures, and
 * checks early on focus or visibility. At most one read is in flight; a hidden
 * tab defers its due check until it is visible. It stops once stale.
 */
export class WebBuildMonitor {
  private readonly options: WebBuildMonitorOptions;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private controller: AbortController | null = null;
  private failures = 0;
  private lastCheck: number;
  private dueWhileHidden = false;
  private finished = false;

  constructor(options: WebBuildMonitorOptions) {
    this.options = options;
    // Page load counts as a fresh read: the bundle just came from the server.
    this.lastCheck = this.now();
  }

  start(): void {
    this.schedule();
  }

  stop(): void {
    this.finished = true;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.controller?.abort();
    this.controller = null;
  }

  /** Window focus or the tab becoming visible. */
  nudge(): void {
    if (this.finished || this.hidden()) return;
    const spacing = this.options.minSpacingMs ?? BUILD_CHECK_MIN_SPACING_MS;
    if (!this.dueWhileHidden && this.now() - this.lastCheck < spacing) return;
    void this.check();
  }

  private now(): number {
    return (this.options.now ?? Date.now)();
  }

  private hidden(): boolean {
    return this.options.isHidden?.() ?? false;
  }

  private schedule(): void {
    if (this.finished) return;
    if (this.timer !== null) clearTimeout(this.timer);
    const interval = this.options.intervalMs ?? BUILD_CHECK_INTERVAL_MS;
    const ceiling = this.options.maxIntervalMs ?? BUILD_CHECK_MAX_INTERVAL_MS;
    const base = Math.min(interval * 2 ** this.failures, ceiling);
    // +/-20% jitter, so tabs opened together don't poll together.
    const jitter = 0.8 + 0.4 * (this.options.random ?? Math.random)();
    this.timer = setTimeout(() => {
      this.timer = null;
      if (this.hidden()) {
        this.dueWhileHidden = true;
        return;
      }
      void this.check();
    }, Math.round(base * jitter));
  }

  private async check(): Promise<void> {
    if (this.finished || this.controller !== null) return;
    this.dueWhileHidden = false;
    this.lastCheck = this.now();
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    const controller = new AbortController();
    this.controller = controller;
    const timeout = setTimeout(
      () => controller.abort(),
      this.options.timeoutMs ?? BUILD_CHECK_TIMEOUT_MS,
    );
    let deployed: string | null = null;
    try {
      deployed = await this.options.fetchBuildId(controller.signal);
    } catch {
      deployed = null;
    } finally {
      clearTimeout(timeout);
      if (this.controller === controller) this.controller = null;
    }
    if (this.finished) return;
    if (deployed === null) {
      this.failures = Math.min(this.failures + 1, 8);
    } else if (deployed !== this.options.runningBuildId) {
      this.finished = true;
      this.options.onStale();
      return;
    } else {
      this.failures = 0;
    }
    this.schedule();
  }
}

/** True once the deployed web build differs from the one this tab is running. */
export function useStaleWebBuild(): boolean {
  const [stale, setStale] = useState(false);
  const running = runningWebBuildId();
  useEffect(() => {
    if (running === null) return;
    const monitor = new WebBuildMonitor({
      runningBuildId: running,
      fetchBuildId: (signal) => fetchDeployedBuildId(signal),
      onStale: () => setStale(true),
      isHidden: () => document.visibilityState === "hidden",
    });
    const nudge = () => monitor.nudge();
    window.addEventListener("focus", nudge);
    document.addEventListener("visibilitychange", nudge);
    monitor.start();
    return () => {
      window.removeEventListener("focus", nudge);
      document.removeEventListener("visibilitychange", nudge);
      monitor.stop();
    };
  }, [running]);
  return stale;
}
