// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Session, ToolConsentSummary } from "@/lib/types";
import { makeChatSession } from "./chatTestFixtures";
import { SessionToolConsentBanner, SessionToolConsentControls } from "./ToolConsentControls";

const mocks = vi.hoisted(() => ({ setSessionToolConsent: vi.fn() }));
vi.mock("@/lib/api", () => ({
  setSessionToolConsent: mocks.setSessionToolConsent,
  apiErrorDetail: (reason: unknown) => reason instanceof Error ? reason.message : "Request failed",
}));

afterEach(() => { cleanup(); vi.clearAllMocks(); vi.useRealTimers(); });

function grant(overrides: Partial<ToolConsentSummary> = {}): ToolConsentSummary {
  return {
    id: "consent-a", scope: "session", grantedAt: new Date().toISOString(),
    expiresAt: new Date(Date.now() + 60_000).toISOString(), toolCount: 7, ...overrides,
  };
}

describe("explicit session tool consent", () => {
  it("keeps the draft option unchecked and explains that a session must be started first", () => {
    const change = vi.fn();
    render(<SessionToolConsentControls sessionId={null} consent={null} available={null}
      canEnable={false} pending={false} error={null} onChange={change} />);
    const checkbox = screen.getByRole("checkbox", { name: "Auto-approve enabled tools for this session" });
    expect(checkbox).not.toBeChecked();
    expect(checkbox).toBeDisabled();
    expect(screen.getByText(/Start the conversation first/)).toBeVisible();
    expect(screen.getByText(/Hostile retrieved content/)).toBeVisible();
    expect(change).not.toHaveBeenCalled();
  });

  it("hides an unavailable opt-in but leaves saved consent revocable when the operator turns it off", async () => {
    const change = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(<SessionToolConsentControls sessionId="s1" consent={null} available={false}
      canEnable={true} pending={false} error={null} onChange={change} />);
    expect(screen.queryByRole("checkbox")).toBeNull();
    rerender(<SessionToolConsentControls sessionId="s1" consent={grant()} available={false}
      canEnable={false} pending={false} error={null} onChange={change} />);
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(screen.getByRole("checkbox")).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Revoke session auto-approval" }));
    expect(change).toHaveBeenCalledWith(false);
  });

  it("expires a visible consent without a new message or reload, and preserves the exact tool count", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-06T15:00:00Z"));
    render(<SessionToolConsentControls sessionId="s1" consent={grant({ toolCount: 0 })} available={true} active={true} status="active"
      canEnable={true} pending={false} error={null} onChange={vi.fn()} />);
    expect(screen.getByRole("checkbox")).toBeChecked();
    expect(screen.getByText(/0 enabled tool contracts in this consent/)).toBeVisible();
    await act(() => vi.advanceTimersByTimeAsync(60_001));
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(screen.getByText("Session auto-approval has expired.")).toBeVisible();
  });

  it("does not report expired or malformed consent as enabled on initial render", () => {
    render(<SessionToolConsentBanner sessionId="s1" consent={grant({ expiresAt: "2020-01-01T00:00:00Z" })}
      available={true} active={true} status="active" onUpdated={vi.fn()} />);
    expect(screen.getByText("Session auto-approval expired")).toBeVisible();
    expect(screen.queryByText("Auto-approval on for this session")).toBeNull();
  });

  it("does not optimistically claim revocation and exposes errors with a retry", async () => {
    mocks.setSessionToolConsent.mockRejectedValue(new Error("Request rejected"));
    const updated = vi.fn();
    render(<SessionToolConsentBanner sessionId="s1" consent={grant()} available={true} active={true} status="active" onUpdated={updated} />);
    await userEvent.click(screen.getByRole("button", { name: "Revoke auto-approval" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Consent has not been confirmed revoked");
    expect(screen.getByText("Auto-approval on for this session")).toBeVisible();
    expect(updated).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Revoke auto-approval" })).toBeEnabled();
  });

  it("keeps revocation single-flight and ignores a late A -> B -> A response", async () => {
    let resolve!: (session: Session) => void;
    mocks.setSessionToolConsent.mockImplementation(() => new Promise<Session>((done) => { resolve = done; }));
    const updated = vi.fn();
    const consent = grant();
    const { rerender } = render(<SessionToolConsentBanner sessionId="A" consent={consent} available={true} active={true} status="active" onUpdated={updated} />);
    fireEvent.click(screen.getByRole("button", { name: "Revoke auto-approval" }));
    fireEvent.click(screen.getByRole("button", { name: "Revoking…" }));
    expect(mocks.setSessionToolConsent).toHaveBeenCalledTimes(1);
    rerender(<SessionToolConsentBanner sessionId="B" consent={consent} available={true} active={true} status="active" onUpdated={updated} />);
    rerender(<SessionToolConsentBanner sessionId="A" consent={consent} available={true} active={true} status="active" onUpdated={updated} />);
    await act(async () => resolve({ ...makeChatSession("A"), toolConsent: null }));
    expect(updated).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Revoke auto-approval" })).toBeEnabled();
  });

  it("ignores a successful completion after unmount", async () => {
    let resolve!: (session: Session) => void;
    mocks.setSessionToolConsent.mockImplementation(() => new Promise<Session>((done) => { resolve = done; }));
    const updated = vi.fn();
    const { unmount } = render(<SessionToolConsentBanner sessionId="A" consent={grant()} available={true} active={true} status="active" onUpdated={updated} />);
    await userEvent.click(screen.getByRole("button", { name: "Revoke auto-approval" }));
    unmount();
    await act(async () => resolve({ ...makeChatSession("A"), toolConsent: null }));
    expect(updated).not.toHaveBeenCalled();
  });

  it("delivers only a successful server response for the currently bound session", async () => {
    const session = { ...makeChatSession("A"), toolConsent: null };
    mocks.setSessionToolConsent.mockResolvedValue(session);
    const updated = vi.fn();
    render(<SessionToolConsentBanner sessionId="A" consent={grant()} available={true} active={true} status="active" onUpdated={updated} />);
    await userEvent.click(screen.getByRole("button", { name: "Revoke auto-approval" }));
    await waitFor(() => expect(updated).toHaveBeenCalledWith(session));
    expect(mocks.setSessionToolConsent).toHaveBeenCalledWith("A", false);
  });
  it("renews changed tool contracts only after another explicit user action", async () => {
    const change = vi.fn().mockResolvedValue(undefined);
    render(<SessionToolConsentControls sessionId="s1" consent={grant()} available={true} active={true} status="active"
      canEnable={true} pending={false} error={null} onChange={change} />);
    expect(change).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Renew consent for current enabled tools" }));
    expect(change).toHaveBeenCalledExactlyOnceWith(true);
  });

  it("does not treat a stored unexpired grant as active without live verification", () => {
    render(<SessionToolConsentControls sessionId="s1" consent={grant()} available={true}
      canEnable={true} pending={false} error={null} onChange={vi.fn()} />);
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(screen.queryByText("Auto-approval is enabled for this session.")).toBeNull();
  });

  it("does not claim active consent when the current contracts have changed", () => {
    render(<SessionToolConsentBanner sessionId="s1" consent={grant()} available={true}
      active={false} status="changed" onUpdated={vi.fn()} />);
    expect(screen.queryByText("Auto-approval on for this session")).toBeNull();
    expect(screen.getByText(/needs renewal/)).toBeVisible();
  });

  it.each(["off", "expired", "revoked", "changed", "disabled", "unavailable"] as const)(
    "never shows active for server status %s, even with an unexpired summary",
    (status) => {
      render(<SessionToolConsentControls sessionId="s1" consent={grant()} available={true}
        active={true} status={status} canEnable={true} pending={false} error={null} onChange={vi.fn()} />);
      expect(screen.getByRole("checkbox")).not.toBeChecked();
      expect(screen.getByRole("button", { name: "Revoke session auto-approval" })).toBeEnabled();
      expect(screen.queryByText("Auto-approval is enabled for this session.")).toBeNull();
    },
  );

  it("drops verified active display on inspection failure while retaining audit and revoke", () => {
    const consent = grant();
    const { rerender } = render(<SessionToolConsentBanner sessionId="s1" consent={consent} available={true}
      active={true} status="active" onUpdated={vi.fn()} onRefresh={vi.fn()} />);
    expect(screen.getByText("Auto-approval on for this session")).toBeVisible();
    rerender(<SessionToolConsentBanner sessionId="s1" consent={consent} available={null}
      active={false} status="unavailable" onUpdated={vi.fn()} onRefresh={vi.fn()} />);
    expect(screen.queryByText("Auto-approval on for this session")).toBeNull();
    expect(screen.getByText(/unverified/)).toBeVisible();
    expect(screen.getByText(/7 enabled tool contracts/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Revoke auto-approval" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Refresh consent status" })).toBeEnabled();
  });

});
