// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { SignInGate } from "./SignInGate";

const mocks = vi.hoisted(() => ({
  authenticated: false,
  inProgress: "none",
  recovery: { status: "idle" } as { status: "idle" | "redirecting" | "error" },
  listeners: new Set<() => void>(),
  loginRedirect: vi.fn(),
  retryRecovery: vi.fn(),
}));

vi.mock("@azure/msal-browser", () => ({
  InteractionStatus: { None: "none" },
}));
vi.mock("@azure/msal-react", () => ({
  useIsAuthenticated: () => mocks.authenticated,
  useMsal: () => ({
    instance: { loginRedirect: mocks.loginRedirect },
    inProgress: mocks.inProgress,
  }),
}));
vi.mock("@/lib/auth", () => ({
  getWebAuthConfig: () => ({
    provider: "entra",
    apiScope: "api://client/access",
  }),
  getAuthRecoveryState: () => mocks.recovery,
  subscribeToAuthRecovery: (listener: () => void) => {
    mocks.listeners.add(listener);
    return () => mocks.listeners.delete(listener);
  },
  retryInteractiveTokenRecovery: mocks.retryRecovery,
}));

beforeEach(() => {
  mocks.authenticated = false;
  mocks.inProgress = "none";
  mocks.recovery = { status: "idle" };
  mocks.loginRedirect.mockReset().mockResolvedValue(undefined);
  mocks.retryRecovery.mockReset().mockRejectedValue(new Error("blocked"));
});
afterEach(() => {
  cleanup();
  mocks.listeners.clear();
});

describe("SignInGate", () => {
  it("hides the authenticated workspace and explains the tenant-specific sign-in", () => {
    render(
      <SignInGate>
        <div>Authenticated workspace</div>
      </SignInGate>,
    );

    expect(screen.queryByText("Authenticated workspace")).toBeNull();
    expect(
      screen.getByText(/Sign in with Microsoft Entra ID/i),
    ).toHaveTextContent(/authorized for this organization's tenant/i);
    expect(screen.getByText(/invited B2B guest accounts/i)).toBeInTheDocument();
  });

  it("admits children when authentication is healthy", () => {
    mocks.authenticated = true;

    render(
      <SignInGate>
        <div>Authenticated workspace</div>
      </SignInGate>,
    );

    expect(screen.getByText("Authenticated workspace")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
  });

  it("surfaces an interactive sign-in redirect failure in an alert", async () => {
    const user = userEvent.setup();
    mocks.loginRedirect.mockRejectedValue(new Error("popup blocked"));
    render(<SignInGate>Authenticated workspace</SignInGate>);

    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "We couldn't redirect you to Microsoft Entra ID.",
    );
    expect(screen.queryByText(/popup blocked/i)).toBeNull();
  });

  it("gates an authenticated-looking session while recovery redirects", () => {
    mocks.authenticated = true;
    mocks.recovery = { status: "redirecting" };

    render(<SignInGate>Authenticated workspace</SignInGate>);

    expect(screen.queryByText("Authenticated workspace")).toBeNull();
    expect(screen.getByText(/session needs attention/i)).toBeInTheDocument();
  });

  it("keeps recovery failures gated and exposes an alert-backed retry", async () => {
    const user = userEvent.setup();
    mocks.authenticated = true;
    mocks.recovery = { status: "error" };

    render(<SignInGate>Authenticated workspace</SignInGate>);

    expect(screen.queryByText("Authenticated workspace")).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "We couldn't redirect you to renew your Microsoft Entra ID session.",
    );
    await user.click(screen.getByRole("button", { name: "Try signing in again" }));
    expect(mocks.retryRecovery).toHaveBeenCalledTimes(1);
  });
});
