// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  installGlobalClientTelemetry: vi.fn(),
}));

vi.mock("@/lib/clientTelemetry", () => ({
  installGlobalClientTelemetry: mocks.installGlobalClientTelemetry,
}));

import { ClientTelemetryBoot } from "./ClientTelemetryBoot";

afterEach(() => {
  cleanup();
  mocks.installGlobalClientTelemetry.mockReset();
});

describe("ClientTelemetryBoot", () => {
  it("installs the global telemetry listeners once on mount and renders nothing", () => {
    const { container } = render(<ClientTelemetryBoot />);

    expect(mocks.installGlobalClientTelemetry).toHaveBeenCalledTimes(1);
    expect(container).toBeEmptyDOMElement();
  });
});
