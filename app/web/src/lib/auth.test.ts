// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  class InteractionRequiredAuthError extends Error {}
  return {
    InteractionRequiredAuthError,
    account: { homeAccountId: "account-1" },
    silent: vi.fn(),
    redirect: vi.fn(),
    fetch: vi.fn(),
    client: {
      getActiveAccount: vi.fn(),
      getAllAccounts: vi.fn(),
      acquireTokenSilent: vi.fn(),
      acquireTokenRedirect: vi.fn(),
    },
  };
});

vi.mock("@azure/msal-browser", () => ({
  InteractionRequiredAuthError: mocks.InteractionRequiredAuthError,
  PublicClientApplication: class {
    constructor() {
      return mocks.client;
    }
  },
}));

const entraConfig = {
  provider: "entra" as const,
  clientId: "client",
  tenantId: "tenant",
  apiScope: "api://client/access",
};

beforeEach(() => {
  vi.resetModules();
  mocks.client.getActiveAccount.mockReset().mockReturnValue(mocks.account);
  mocks.client.getAllAccounts.mockReset().mockReturnValue([mocks.account]);
  mocks.client.acquireTokenSilent.mockReset();
  mocks.client.acquireTokenRedirect.mockReset();
  mocks.fetch.mockReset().mockResolvedValue(new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", mocks.fetch);
});

describe("Entra API token acquisition", () => {
  it("attaches a silently acquired token before issuing the API request", async () => {
    mocks.client.acquireTokenSilent.mockResolvedValue({ accessToken: "access-token" });
    const { apiFetch, initAuth } = await import("./auth");
    initAuth(entraConfig);

    await apiFetch("/api/models");

    expect(mocks.fetch).toHaveBeenCalledTimes(1);
    const [, init] = mocks.fetch.mock.calls[0];
    expect(new Headers(init?.headers).get("Authorization")).toBe(
      "Bearer access-token",
    );
    expect(mocks.client.acquireTokenRedirect).not.toHaveBeenCalled();
  });

  it("single-flights interaction-required redirects and never sends an anonymous request", async () => {
    let rejectRedirect!: (error: Error) => void;
    mocks.client.acquireTokenSilent.mockRejectedValue(
      new mocks.InteractionRequiredAuthError(),
    );
    mocks.client.acquireTokenRedirect.mockImplementation(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectRedirect = reject;
        }),
    );
    const {
      apiFetch,
      getAuthRecoveryState,
      initAuth,
      retryInteractiveTokenRecovery,
    } = await import("./auth");
    initAuth(entraConfig);

    const first = apiFetch("/api/models");
    const second = apiFetch("/api/sessions");
    await vi.waitFor(() =>
      expect(mocks.client.acquireTokenRedirect).toHaveBeenCalledTimes(1),
    );
    expect(getAuthRecoveryState()).toEqual({ status: "redirecting" });
    expect(mocks.fetch).not.toHaveBeenCalled();

    rejectRedirect(new Error("redirect blocked"));
    await expect(first).rejects.toMatchObject({ name: "AuthenticationRecoveryError" });
    await expect(second).rejects.toMatchObject({ name: "AuthenticationRecoveryError" });
    expect(getAuthRecoveryState()).toEqual({ status: "error" });

    await expect(apiFetch("/api/tools")).rejects.toMatchObject({
      name: "AuthenticationRecoveryError",
    });
    expect(mocks.client.acquireTokenRedirect).toHaveBeenCalledTimes(1);
    expect(mocks.fetch).not.toHaveBeenCalled();

    mocks.client.acquireTokenRedirect.mockRejectedValueOnce(
      new Error("redirect still blocked"),
    );
    await expect(retryInteractiveTokenRecovery()).rejects.toMatchObject({
      name: "AuthenticationRecoveryError",
    });
    expect(mocks.client.acquireTokenRedirect).toHaveBeenCalledTimes(2);
  });
});
