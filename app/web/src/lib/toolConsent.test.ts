import { describe, expect, it } from "vitest";
import type { ToolConsentSummary } from "./types";
import { inspectedSessionConsent, unverifiedSessionConsent } from "./toolConsent";
import { makeInspectorSnapshot } from "../components/chatTestFixtures";

const grant: ToolConsentSummary = { id: "grant", scope: "session", grantedAt: "2026-09-06T00:00:00Z", expiresAt: "2099-09-06T08:00:00Z", toolCount: 11 };

describe("live Inspector consent projection", () => {
  it("requires every active proof field, not just a summary or availability", () => {
    const base = { ...makeInspectorSnapshot("s1"), toolAutoApproveAvailable: true, toolConsent: grant,
      toolConsentActive: true, toolConsentStatus: "active" as const };
    expect(inspectedSessionConsent(base).active).toBe(true);
    expect(inspectedSessionConsent({ ...base, toolConsentActive: false }).active).toBe(false);
    expect(inspectedSessionConsent({ ...base, toolConsentStatus: "changed" }).active).toBe(false);
    expect(inspectedSessionConsent({ ...base, toolAutoApproveAvailable: false }).active).toBe(false);
    expect(inspectedSessionConsent({ ...base, toolConsent: null }).active).toBe(false);
    expect(inspectedSessionConsent({ ...base, toolConsent: { ...grant, scope: "run" } }).active).toBe(false);
  });

  it("fails closed for older responses without activation/status fields", () => {
    const old = { ...makeInspectorSnapshot("s1"), toolAutoApproveAvailable: true, toolConsent: grant };
    delete old.toolConsentActive;
    delete old.toolConsentStatus;
    expect(inspectedSessionConsent(old)).toMatchObject({ active: false, status: "unavailable", consent: grant });
  });

  it("distinguishes explicit no-consent from an unavailable audit-only fallback", () => {
    expect(inspectedSessionConsent(makeInspectorSnapshot("s1")).consent).toBeNull();
    expect(unverifiedSessionConsent()).toEqual({ available: null, active: false, status: null, consent: undefined });
    expect(unverifiedSessionConsent("unavailable")).toMatchObject({ active: false, status: "unavailable" });
  });
});
