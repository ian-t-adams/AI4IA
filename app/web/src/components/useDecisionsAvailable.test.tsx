// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { decisionModels, type DecisionModel } from "@/lib/decisions";
import { useDecisionsAvailable } from "./useDecisionsAvailable";

vi.mock("@/lib/decisions", () => ({ decisionModels: vi.fn() }));
const available = [{ id: "decision", displayName: "Decision", residencies: ["us"] }];

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  vi.restoreAllMocks();
});

describe("Decision navigation availability", () => {
  it.each([{ models: [] }, { models: available }])("uses only the authenticated eligible models ($models)", async ({ models }) => {
    vi.mocked(decisionModels).mockResolvedValue(models);
    const { result } = renderHook(() => useDecisionsAvailable("owner-a"));
    expect(result.current).toBe(false);
    await act(async () => {});
    expect(result.current).toBe(models.length > 0);
    expect(decisionModels).toHaveBeenCalledExactlyOnceWith(expect.any(AbortSignal));
  });

  it("hides unavailable reads and reports the failure without payloads", async () => {
    const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.mocked(decisionModels).mockRejectedValue(new Error("Unavailable"));
    const { result } = renderHook(() => useDecisionsAvailable("owner-a"));
    await waitFor(() => expect(warning).toHaveBeenCalledOnce());
    expect(result.current).toBe(false);
    expect(warning.mock.calls[0]).toHaveLength(1);
  });

  it("does not read options without an owner", () => {
    const { result } = renderHook(() => useDecisionsAvailable(null));
    expect(result.current).toBe(false);
    expect(decisionModels).not.toHaveBeenCalled();
  });

  it("clears visibility on account changes and ignores late responses after abort", async () => {
    let finish!: (models: DecisionModel[]) => void;
    vi.mocked(decisionModels).mockResolvedValueOnce(available)
      .mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }))
      .mockResolvedValueOnce([]);
    const { result, rerender, unmount } = renderHook(
      ({ owner }: { owner: string | null }) => useDecisionsAvailable(owner),
      { initialProps: { owner: "owner-a" as string | null } },
    );
    await waitFor(() => expect(result.current).toBe(true));
    rerender({ owner: "owner-b" });
    expect(result.current).toBe(false);
    const signal = vi.mocked(decisionModels).mock.calls[1][0];
    rerender({ owner: "owner-c" });
    expect(signal.aborted).toBe(true);
    await act(async () => { finish(available); });
    expect(result.current).toBe(false);
    rerender({ owner: null });
    expect(result.current).toBe(false);
    unmount();
    expect(vi.mocked(decisionModels).mock.calls[2][0].aborted).toBe(true);
  });
});
