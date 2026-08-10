// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AuthenticatedChatApp } from "./AuthenticatedChatApp";
import { SignInGate } from "./SignInGate";
import { SkipLink } from "./SkipLink";

const mocks = vi.hoisted(() => ({
  authenticated: false,
  dynamicLoader: vi.fn(),
  recovery: { status: "idle" },
  revealListeners: new Set<() => void>(),
}));

vi.mock("next/dynamic", async () => {
  const React = await import("react");
  return {
    default: (
      loader: () => Promise<unknown>,
      options: { loading: React.ComponentType },
    ) =>
      function TestDynamicComponent() {
        const [revealed, setRevealed] = React.useState(false);
        React.useEffect(() => {
          mocks.dynamicLoader();
          void loader();
          const reveal = () => setRevealed(true);
          mocks.revealListeners.add(reveal);
          return () => {
            mocks.revealListeners.delete(reveal);
          };
        }, []);
        if (!revealed) return <options.loading />;
        return <main id="main">Loaded authenticated workspace</main>;
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
  mocks.revealListeners.clear();
});
afterEach(() => {
  cleanup();
  mocks.revealListeners.clear();
});

function revealWorkspace() {
  act(() => {
    for (const reveal of mocks.revealListeners) reveal();
  });
}

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

    expect(screen.getByText("Loading workspace…")).toBeInTheDocument();
    await waitFor(() => expect(mocks.dynamicLoader).toHaveBeenCalledTimes(1));
    revealWorkspace();
    expect(screen.getByText("Loaded authenticated workspace")).toBeInTheDocument();
  });

  it("transfers skip-link focus from the loading main to the revealed main", async () => {
    const user = userEvent.setup();
    mocks.authenticated = true;
    render(
      <>
        <SkipLink />
        <AuthenticatedChatApp />
      </>,
    );
    await waitFor(() => expect(mocks.dynamicLoader).toHaveBeenCalledTimes(1));

    await user.click(screen.getByRole("link", { name: "Skip to main content" }));
    const loadingMain = screen.getByRole("main");
    expect(loadingMain).toHaveTextContent("Loading workspace…");
    expect(loadingMain).toHaveFocus();

    revealWorkspace();

    const revealedMain = screen.getByRole("main");
    await waitFor(() => expect(revealedMain).toHaveFocus());
    expect(revealedMain).toHaveTextContent("Loaded authenticated workspace");
  });

  it("does not steal focus on reveal when the loading main was not focused", async () => {
    const user = userEvent.setup();
    mocks.authenticated = true;
    render(
      <>
        <button type="button">Keep focus here</button>
        <AuthenticatedChatApp />
      </>,
    );
    await waitFor(() => expect(mocks.dynamicLoader).toHaveBeenCalledTimes(1));
    const button = screen.getByRole("button", { name: "Keep focus here" });
    await user.click(button);
    expect(button).toHaveFocus();

    revealWorkspace();

    await waitFor(() => expect(screen.getByRole("main")).toBeInTheDocument());
    expect(button).toHaveFocus();
  });
});
