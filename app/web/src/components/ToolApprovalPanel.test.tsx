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
    argumentsMasked: [],
    argumentsElided: [],
    argumentsOmitted: 0,
    grantHash: "h".repeat(64),
    consumed: false,
    expiresAt: "2099-08-05T13:10:00Z",
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
    expect(screen.getByRole("button", { name: /Approve and retry/ })).toBeInTheDocument();
    expect(screen.getByText(/model must re-issue this exact call/i)).toBeInTheDocument();
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

  it("disables an expired approval and explains how to recover", () => {
    render(
      <ToolApprovalPanel
        prompts={[prompt({ expiresAt: "2000-01-01T00:00:00Z" })]}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /Approve and retry/ })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent(/expired/i);
    expect(screen.getByRole("button", { name: /^Deny/ })).toBeEnabled();
  });

  it("warns loudly when the card is not the whole call", () => {
    // The argument set is model-controlled, so a silently-shortened preview is
    // how an exfiltration's destination gets hidden from the approver. If the
    // server could not show everything, the card must say so unmissably.
    render(
      <ToolApprovalPanel
        prompts={[prompt({ argumentsOmitted: 7 })]}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
      />,
    );
    const warning = screen.getByRole("alert");
    expect(warning).toHaveTextContent(/7 more arguments will be sent/);
    expect(warning).toHaveTextContent(/not seeing the whole call/);
  });

  it("stays quiet when it is showing everything", () => {
    render(
      <ToolApprovalPanel
        prompts={[prompt()]}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
      />,
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("marks a masked value as hidden rather than as the value", () => {
    // `***REDACTED***` means "hidden from you, but sent in full" — the user
    // must not read it as the literal content of the outbound call.
    render(
      <ToolApprovalPanel
        prompts={[
          prompt({
            argumentsPreview: { api_key: "***REDACTED***" },
            argumentsMasked: ["api_key"],
          }),
        ]}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
      />,
    );
    expect(screen.getByText(/hidden here, sent in full/)).toBeInTheDocument();
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
