// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

import { AuthenticatedChatApp } from "./AuthenticatedChatApp";
import { SignInGate } from "./SignInGate";

const mocks = vi.hoisted(() => ({
  authenticated: false,
  dynamicLoader: vi.fn(),
  recovery: { status: "idle" },
}));

vi.mock("next/dynamic", async () => {
  const React = await import("react");
  return {
    default: (loader: () => Promise<unknown>) =>
      function TestDynamicComponent() {
        React.useEffect(() => {
          mocks.dynamicLoader();
          void loader();
        }, []);
        return <div>Loaded authenticated workspace</div>;
      },
  };
});
vi.mock("@azure/msal-browser", () => ({
  InteractionStatus: { None: "none" },
}));
vi.mock("@azure/msal-react", () => ({
  useIsAuthenticated: () => mocks.authenticated,
  useMsal: () => ({
    instance: { loginRedirect: vi.fn() },
    inProgress: "none",
  }),
}));
vi.mock("@/lib/auth", () => ({
  getWebAuthConfig: () => ({
    provider: "entra",
    apiScope: "api://client/access",
  }),
  getAuthRecoveryState: () => mocks.recovery,
  subscribeToAuthRecovery: () => () => undefined,
  retryInteractiveTokenRecovery: vi.fn(),
}));
vi.mock("./ChatApp", () => ({
  ChatApp: () => <div>Chat application module</div>,
}));

beforeEach(() => {
  mocks.authenticated = false;
  mocks.dynamicLoader.mockReset();
});
afterEach(cleanup);

describe("authenticated workspace bundle boundary", () => {
  it("does not invoke the ChatApp dynamic loader while signed out", () => {
    render(
      <SignInGate>
        <AuthenticatedChatApp />
      </SignInGate>,
    );

    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(mocks.dynamicLoader).not.toHaveBeenCalled();
  });

  it("invokes the ChatApp dynamic loader after the gate admits children", async () => {
    mocks.authenticated = true;

    render(
      <SignInGate>
        <AuthenticatedChatApp />
      </SignInGate>,
    );

    expect(screen.getByText("Loaded authenticated workspace")).toBeInTheDocument();
    await waitFor(() => expect(mocks.dynamicLoader).toHaveBeenCalledTimes(1));
  });
});
