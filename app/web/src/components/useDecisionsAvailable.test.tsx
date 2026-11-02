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
    expect(result.current.status).toBe("loading");
    await act(async () => {});
    expect(result.current.status).toBe(models.length > 0 ? "available" : "unavailable");
    expect(decisionModels).toHaveBeenCalledExactlyOnceWith(expect.any(AbortSignal));
  });

  it("distinguishes failed reads from absence and recovers after retry", async () => {
    vi.mocked(decisionModels).mockRejectedValueOnce(new Error("Unavailable"))
      .mockResolvedValueOnce(available);
    const { result } = renderHook(() => useDecisionsAvailable("owner-a"));
    await waitFor(() => expect(result.current.status).toBe("error"));
    act(() => result.current.retry());
    expect(result.current.status).toBe("loading");
    await waitFor(() => expect(result.current.status).toBe("available"));
    expect(decisionModels).toHaveBeenCalledTimes(2);
  });

  it("does not read options without an owner", () => {
    const { result } = renderHook(() => useDecisionsAvailable(null));
    expect(result.current.status).toBe("unavailable");
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
    await waitFor(() => expect(result.current.status).toBe("available"));
    rerender({ owner: "owner-b" });
    expect(result.current.status).toBe("loading");
    const signal = vi.mocked(decisionModels).mock.calls[1][0];
    rerender({ owner: "owner-c" });
    expect(signal.aborted).toBe(true);
    await act(async () => { finish(available); });
    expect(result.current.status).toBe("unavailable");
    rerender({ owner: null });
    expect(result.current.status).toBe("unavailable");
    unmount();
    expect(vi.mocked(decisionModels).mock.calls[2][0].aborted).toBe(true);
  });
});
