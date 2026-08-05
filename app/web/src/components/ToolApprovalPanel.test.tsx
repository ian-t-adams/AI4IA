// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ToolApprovalPanel } from "./ToolApprovalPanel";
import type { PendingToolApprovalPrompt } from "../lib/types";

afterEach(cleanup);

function prompt(
  overrides: Partial<PendingToolApprovalPrompt> = {},
): PendingToolApprovalPrompt {
  return {
    id: "req-1",
    tool: "mcp_courier_abc123",
    label: "mcp:courier/send",
    host: "courier.example.com",
    purpose: "Send a message to a recipient",
    risk: "external",
    argumentsDigest: "d".repeat(64),
    argumentsPreview: { to: "attacker@evil.example", body: "quarterly figures" },
    grantHash: "h".repeat(64),
    consumed: false,
    expiresAt: "2026-08-05T13:10:00Z",
    createdAt: "2026-08-05T13:00:00Z",
    grant: "one-time-grant",
    ...overrides,
  };
}

describe("ToolApprovalPanel", () => {
  it("renders nothing when there is nothing to approve", () => {
    const { container } = render(
      <ToolApprovalPanel prompts={[]} onApprove={vi.fn()} onDeny={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the destination host and the redacted argument preview", () => {
    // The host is the field an exfiltration attempt has to change and cannot
    // disguise, so it must be visible without expanding anything.
    render(
      <ToolApprovalPanel
        prompts={[prompt()]}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
      />,
    );
    expect(screen.getByText("sends to courier.example.com")).toBeInTheDocument();
    expect(screen.getByText("mcp:courier/send")).toBeInTheDocument();
    expect(screen.getByText("attacker@evil.example")).toBeInTheDocument();
    expect(screen.getByText("quarterly figures")).toBeInTheDocument();
  });

  it("does not claim a single destination when the server named none", () => {
    render(
      <ToolApprovalPanel
        prompts={[prompt({ host: null })]}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
      />,
    );
    expect(screen.queryByText(/sends to/)).not.toBeInTheDocument();
  });

  it("passes the whole prompt back on approve so the caller sends the grant", async () => {
    const onApprove = vi.fn();
    const user = userEvent.setup();
    const pending = prompt();
    render(
      <ToolApprovalPanel
        prompts={[pending]}
        onApprove={onApprove}
        onDeny={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: /^Approve/ }));
    expect(onApprove).toHaveBeenCalledWith(pending);
  });

  it("denies locally without needing a server round trip", async () => {
    const onApprove = vi.fn();
    const onDeny = vi.fn();
    const user = userEvent.setup();
    render(
      <ToolApprovalPanel
        prompts={[prompt()]}
        onApprove={onApprove}
        onDeny={onDeny}
      />,
    );
    await user.click(screen.getByRole("button", { name: /^Deny/ }));
    expect(onDeny).toHaveBeenCalledTimes(1);
    expect(onApprove).not.toHaveBeenCalled();
  });

  it("disables both actions while a turn is in flight", () => {
    render(
      <ToolApprovalPanel
        prompts={[prompt()]}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
        busy
      />,
    );
    expect(screen.getByRole("button", { name: /^Approve/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^Deny/ })).toBeDisabled();
  });

  it("renders one card per held call with distinct labels", () => {
    render(
      <ToolApprovalPanel
        prompts={[
          prompt(),
          prompt({ id: "req-2", label: "mcp:courier/delete", host: "b.example.com" }),
        ]}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("group", { name: "Approve mcp:courier/send" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("group", { name: "Approve mcp:courier/delete" }),
    ).toBeInTheDocument();
  });
});
