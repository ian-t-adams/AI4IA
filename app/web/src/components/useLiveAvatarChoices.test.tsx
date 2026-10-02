// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, renderHook, waitFor } from "@testing-library/react";

import type { PhotoAvatar, PhotoAvatarConfig } from "@/lib/photoAvatars";
import { useLiveAvatarChoices } from "./useLiveAvatarChoices";

const mocks = vi.hoisted(() => ({
  getPhotoAvatarConfig: vi.fn(),
  listPhotoAvatars: vi.fn(),
}));

vi.mock("@/lib/photoAvatars", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/photoAvatars")>();
  return { ...actual, ...mocks };
});

const ENABLED: PhotoAvatarConfig = {
  enabled: true,
  available: true,
  reason: "available",
  canCreate: true,
  limits: null,
  attributes: null,
  attestation: null,
  disclosure: { label: "AI-generated", text: "A synthetic likeness." },
  pricing: null,
  feedback: null,
};

function avatar(id: string, overrides: Partial<PhotoAvatar> = {}): PhotoAvatar {
  return {
    id,
    displayName: `Host ${id.slice(0, 2)}`,
    prompt: "A friendly host.",
    attributes: { gender: null, age: null, ethnicity: null, style: null },
    status: "ready",
    failure: null,
    preview: null,
    disclosure: { aiGenerated: true, label: "AI-generated" },
    cost: { currency: "USD", estimatedUsd: 2, known: true, priceVersion: "v1", basis: "per_avatar" },
    usable: true,
    reported: false,
    createdAt: "2026-09-26T12:00:00Z",
    updatedAt: "2026-09-26T12:00:00Z",
    readyAt: "2026-09-26T12:00:00Z",
    ...overrides,
  };
}

const READY = avatar("aa".repeat(16));
const REVERIFYING = avatar("bb".repeat(16), { usable: false });
const GENERATING = avatar("cc".repeat(16), { status: "generating", usable: false });

beforeEach(() => {
  mocks.getPhotoAvatarConfig.mockResolvedValue(ENABLED);
  mocks.listPhotoAvatars.mockResolvedValue([READY, REVERIFYING, GENERATING]);
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("useLiveAvatarChoices", () => {
  it("offers only usable avatars while the feature is enabled and available", async () => {
    const { result } = renderHook(() => useLiveAvatarChoices("owner-1", true));
    await waitFor(() => expect(result.current.avatars).toHaveLength(1));
    expect(result.current.avatars[0].id).toBe(READY.id);
    expect(result.current.disclosureLabel).toBe("AI-generated");
  });

  it.each([
    ["disabled", { ...ENABLED, enabled: false }],
    ["unavailable", { ...ENABLED, available: false, reason: "capability_unavailable" as const }],
  ])("offers nothing and lists nothing while %s", async (_label, config) => {
    mocks.getPhotoAvatarConfig.mockResolvedValue(config);
    const { result } = renderHook(() => useLiveAvatarChoices("owner-1", true));
    await waitFor(() => expect(mocks.getPhotoAvatarConfig).toHaveBeenCalledTimes(1));
    await Promise.resolve();
    expect(result.current.avatars).toEqual([]);
    expect(mocks.listPhotoAvatars).not.toHaveBeenCalled();
  });

  it("offers nothing when no avatar is usable or the list cannot be read", async () => {
    mocks.listPhotoAvatars.mockResolvedValueOnce([REVERIFYING, GENERATING]);
    const first = renderHook(() => useLiveAvatarChoices("owner-1", true));
    await waitFor(() => expect(mocks.listPhotoAvatars).toHaveBeenCalledTimes(1));
    expect(first.result.current.avatars).toEqual([]);
    first.unmount();
    mocks.listPhotoAvatars.mockRejectedValueOnce(new Error("offline"));
    const second = renderHook(() => useLiveAvatarChoices("owner-1", true));
    await waitFor(() => expect(mocks.listPhotoAvatars).toHaveBeenCalledTimes(2));
    expect(second.result.current.avatars).toEqual([]);
  });

  it("asks nothing while live voice or the owner is unavailable", () => {
    renderHook(() => useLiveAvatarChoices("owner-1", false));
    renderHook(() => useLiveAvatarChoices(null, true));
    expect(mocks.getPhotoAvatarConfig).not.toHaveBeenCalled();
  });

  it("invalidates the previous snapshot while refreshing the same owner's avatars", async () => {
    const { result, rerender } = renderHook(
      ({ refresh }) => useLiveAvatarChoices("owner-1", true, refresh),
      { initialProps: { refresh: 0 } },
    );
    await waitFor(() => expect(result.current.avatars).toEqual([READY]));
    let finish!: (avatars: PhotoAvatar[]) => void;
    mocks.listPhotoAvatars.mockImplementationOnce(() =>
      new Promise<PhotoAvatar[]>((resolve) => { finish = resolve; }),
    );
    rerender({ refresh: 1 });
    expect(result.current.loading).toBe(true);
    expect(result.current.avatars).toEqual([]);
    await waitFor(() => expect(mocks.listPhotoAvatars).toHaveBeenCalledTimes(2));
    finish([GENERATING]);
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.avatars).toEqual([]);
  });

  it("surfaces a failed refresh and recovers only after a successful read", async () => {
    const { result, rerender } = renderHook(
      ({ refresh }) => useLiveAvatarChoices("owner-1", true, refresh),
      { initialProps: { refresh: 0 } },
    );
    await waitFor(() => expect(result.current.avatars).toEqual([READY]));
    mocks.listPhotoAvatars.mockRejectedValueOnce(new Error("offline"));
    rerender({ refresh: 1 });
    await waitFor(() => expect(result.current.error).toBeTruthy());
    expect(result.current.loading).toBe(false);
    expect(result.current.avatars).toEqual([]);
    rerender({ refresh: 2 });
    await waitFor(() => expect(result.current.avatars).toEqual([READY]));
    expect(result.current.error).toBeNull();
  });

  it("does not show another owner's avatars or accept an abandoned owner's response", async () => {
    const other = avatar("dd".repeat(16), { displayName: "Other owner's host" });
    const { result, rerender } = renderHook(
      ({ owner }) => useLiveAvatarChoices(owner, true),
      { initialProps: { owner: "owner-1" } },
    );
    await waitFor(() => expect(result.current.avatars).toEqual([READY]));
    let finish!: (avatars: PhotoAvatar[]) => void;
    mocks.listPhotoAvatars.mockImplementationOnce(() =>
      new Promise<PhotoAvatar[]>((resolve) => { finish = resolve; }),
    );
    rerender({ owner: "owner-2" });
    expect(result.current.avatars).toEqual([]);
    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(mocks.listPhotoAvatars).toHaveBeenCalledTimes(2));
    rerender({ owner: "owner-3" });
    await waitFor(() => expect(mocks.listPhotoAvatars).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(result.current.avatars).toEqual([READY]));
    finish([other]);
    await Promise.resolve();
    expect(result.current.avatars).toEqual([READY]);
  });
});
