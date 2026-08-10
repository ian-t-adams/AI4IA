// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { UserMenu } from "./UserMenu";

const mocks = vi.hoisted(() => ({
  logoutRedirect: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("@/lib/auth", () => ({
  isEntraEnabled: () => true,
}));

vi.mock("@azure/msal-react", () => ({
  useMsal: () => ({
    instance: {
      getActiveAccount: () => ({
        name: "Ada Lovelace",
        username: "ada@example.com",
      }),
      logoutRedirect: mocks.logoutRedirect,
    },
    accounts: [],
  }),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("UserMenu sign-out", () => {
  it("runs local cleanup before invoking the existing logout redirect", async () => {
    const order: string[] = [];
    mocks.logoutRedirect.mockImplementationOnce(async () => {
      order.push("logout");
    });
    const user = userEvent.setup();
    render(
      <UserMenu
        onBeforeSignOut={() => {
          order.push("cleanup");
          return true;
        }}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Sign out" }));

    expect(order).toEqual(["cleanup", "logout"]);
  });

  it("does not logout when local cleanup declines confirmation", async () => {
    const user = userEvent.setup();
    render(<UserMenu onBeforeSignOut={() => false} />);

    await user.click(screen.getByRole("button", { name: "Sign out" }));

    expect(mocks.logoutRedirect).not.toHaveBeenCalled();
  });
});
