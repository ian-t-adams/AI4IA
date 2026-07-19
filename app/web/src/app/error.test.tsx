// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  reportClientEvent: vi.fn(),
}));

vi.mock("@/lib/clientTelemetry", () => ({
  reportClientEvent: mocks.reportClientEvent,
}));

// Import as `ErrorBoundary` (the default export is the component under test,
// named `Error` in error.tsx per the Next.js App Router convention) so it
// never collides with the native `Error` constructor used below.
import ErrorBoundary from "./error";

afterEach(() => {
  cleanup();
  mocks.reportClientEvent.mockReset();
});

describe("Error boundary", () => {
  it("reports a content-free render error (code + hasDigest only, never the message) and shows a retry action", () => {
    // The message text is intentionally hostile-shaped (mimics a credential
    // leaking into an Error message) using a low-entropy repeated-character
    // placeholder, not a realistic-looking secret, to prove the reported
    // event never carries it (only `code`/`hasDigest` are sent, asserted
    // below).
    const error = Object.assign(new Error(`boom, credentials: Basic ${"z".repeat(12)}`), {
      digest: "abc123",
    });
    const reset = vi.fn();

    render(<ErrorBoundary error={error} reset={reset} />);

    expect(mocks.reportClientEvent).toHaveBeenCalledWith("render_error", {
      code: "Error",
      hasDigest: true,
    });
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText(/Digest: abc123/)).toBeInTheDocument();

    screen.getByRole("button", { name: /retry/i }).click();
    expect(reset).toHaveBeenCalledTimes(1);
  });

  it("reports hasDigest: false when Next.js did not attach a digest", () => {
    const error = new TypeError("boom");
    render(<ErrorBoundary error={error} reset={vi.fn()} />);

    expect(mocks.reportClientEvent).toHaveBeenCalledWith("render_error", {
      code: "TypeError",
      hasDigest: false,
    });
    expect(screen.queryByText(/Digest:/)).not.toBeInTheDocument();
  });
});
