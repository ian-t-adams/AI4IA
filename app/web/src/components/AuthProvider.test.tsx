// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { PublicClientApplication } from "@azure/msal-browser";

import { AuthProvider } from "./AuthProvider";
import type { WebAuthConfig } from "@/lib/auth";

const mocks = vi.hoisted(() => ({ initAuth: vi.fn() }));

vi.mock("@/lib/auth", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/auth")>();
  return { ...original, initAuth: mocks.initAuth };
});
vi.mock("@azure/msal-react", () => ({
  MsalProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("./SignInGate", () => ({
  SignInGate: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

const config: WebAuthConfig = {
  provider: "entra",
  clientId: "client",
  tenantId: "tenant",
  apiScope: "scope",
};

function authClient() {
  return {
    initialize: vi.fn().mockResolvedValue(undefined),
    handleRedirectPromise: vi.fn().mockResolvedValue(null),
    getActiveAccount: vi.fn().mockReturnValue(null),
    getAllAccounts: vi.fn().mockReturnValue([]),
    setActiveAccount: vi.fn(),
    addEventCallback: vi.fn().mockReturnValue("callback-1"),
    removeEventCallback: vi.fn(),
  } as unknown as PublicClientApplication;
}

beforeEach(() => mocks.initAuth.mockReset());
afterEach(cleanup);

describe("AuthProvider Entra initialization", () => {
  it("shows a safe failure state and retries initialization", async () => {
    const user = userEvent.setup();
    const msal = authClient();
    vi.mocked(msal.initialize)
      .mockRejectedValueOnce(new Error("sensitive tenant details"))
      .mockResolvedValueOnce(undefined);
    mocks.initAuth.mockReturnValue(msal);

    render(
      <AuthProvider config={config}>
        <div>Protected application</div>
      </AuthProvider>,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "We couldn't finish signing you in.",
    );
    expect(screen.queryByText(/sensitive tenant details/i)).toBeNull();

    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Protected application")).toBeInTheDocument();
    expect(msal.initialize).toHaveBeenCalledTimes(2);
  });

  it("removes the MSAL event callback when the provider unmounts", async () => {
    const msal = authClient();
    mocks.initAuth.mockReturnValue(msal);
    const { unmount } = render(
      <AuthProvider config={config}>
        <div>Protected application</div>
      </AuthProvider>,
    );

    await screen.findByText("Protected application");
    expect(msal.addEventCallback).toHaveBeenCalledTimes(1);
    unmount();

    await waitFor(() =>
      expect(msal.removeEventCallback).toHaveBeenCalledWith("callback-1"),
    );
  });
});
