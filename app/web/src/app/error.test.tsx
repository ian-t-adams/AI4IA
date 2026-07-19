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
  it("reports the render error to the telemetry bridge and shows a retry action", () => {
    const error = Object.assign(new Error("boom"), { digest: "abc123" });
    const reset = vi.fn();

    render(<ErrorBoundary error={error} reset={reset} />);

    expect(mocks.reportClientEvent).toHaveBeenCalledWith("render_error", {
      message: "boom",
      code: "Error",
      route: expect.any(String),
    });
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText(/Digest: abc123/)).toBeInTheDocument();

    screen.getByRole("button", { name: /retry/i }).click();
    expect(reset).toHaveBeenCalledTimes(1);
  });
});
