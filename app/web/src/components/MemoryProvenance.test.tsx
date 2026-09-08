// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ExecutionReceipt } from "@/lib/types";
import { MemoryProvenance } from "./MemoryProvenance";

function receipt(overrides: Partial<ExecutionReceipt> = {}): ExecutionReceipt {
  return {
    version: 1, runtime: {}, prompt: [], promptMessageCount: 1, promptBytes: 40,
    contextBlocks: [{
      kind: "memory", admitted: true,
      content: { text: "Snapshot: concise answers", sha256: "abc", bytes: 40, truncated: false },
      sources: [{ id: "owned-memory", version: "2", label: "A current label must not replace the snapshot" }],
      sourceCount: 1,
    }],
    droppedHistoryMessages: 0, droppedContextBlocks: [], toolsOffered: [],
    toolsOfferedCount: 0, toolCalls: [], toolCallCount: 0, approvalsRequested: 0,
    approvalsGranted: 0, iterations: 1, status: "complete", partial: false,
    truncated: false, notes: [], ...overrides,
  };
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

async function expand() {
  await userEvent.setup().click(screen.getByText(/^Memories supplied/));
}

describe("MemoryProvenance", () => {
  it("stays collapsed and renders only recorded snapshots, not current memory labels", async () => {
    const fetch = vi.fn();
    const inspect = vi.fn();
    vi.stubGlobal("fetch", fetch);
    render(<MemoryProvenance receipt={receipt()} onInspectMemory={inspect} />);
    expect(screen.getByText("Memories supplied").closest("details")).not.toHaveAttribute("open");
    expect(screen.queryByText("Snapshot: concise answers")).not.toBeInTheDocument();
    await expand();
    expect(await screen.findByText("Snapshot: concise answers")).toBeVisible();
    expect(screen.queryByText(/A current label/)).not.toBeInTheDocument();
    expect(screen.getByText(/not proof it influenced a sentence/)).toBeVisible();
    await userEvent.setup().click(screen.getByRole("button", { name: "Inspect memory owned-memory" }));
    expect(inspect).toHaveBeenCalledWith("owned-memory");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("labels old messages unrecorded without backfilling or inventing references", async () => {
    render(<MemoryProvenance onInspectMemory={vi.fn()} />);
    await expand();
    expect(await screen.findByText(/has not been backfilled/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /^Inspect memory/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /full execution receipt/ })).not.toBeInTheDocument();
  });

  it.each([false, true])("only exposes the supplied block when admitted=%s", async (admitted) => {
    const value = receipt();
    value.contextBlocks[0].admitted = admitted;
    render(<MemoryProvenance receipt={value} onInspectMemory={vi.fn()} />);
    await expand();
    if (admitted) {
      expect(await screen.findByText("Snapshot: concise answers")).toBeVisible();
      expect(screen.getByRole("button", { name: /^Inspect memory/ })).toBeVisible();
    } else {
      expect(await screen.findByText(/was withheld/)).toBeVisible();
      expect(screen.queryByText("Snapshot: concise answers")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /^Inspect memory/ })).not.toBeInTheDocument();
    }
  });

  it("reports shed bodies and references rather than inferring item links", async () => {
    const value = receipt({ truncated: true });
    value.contextBlocks[0].content = { text: "", bytes: 5000, sha256: "abc", truncated: true };
    value.contextBlocks[0].sources = [];
    render(<MemoryProvenance receipt={value} onInspectMemory={vi.fn()} />);
    await expand();
    expect(await screen.findByText("The context body was not retained.")).toBeVisible();
    expect(screen.getByText(/source references were omitted/)).toBeVisible();
    expect(screen.getByText(/missing details do not prove memory was unused/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /^Inspect memory/ })).not.toBeInTheDocument();
  });

  it("separates tool returns from proof of model delivery", async () => {
    render(<MemoryProvenance receipt={receipt({
      contextBlocks: [], toolCallCount: 1, toolCalls: [{
        tool: "recall_memory", outcome: "result",
        result: { text: "A bounded memory tool result", bytes: 50, sha256: "abc", truncated: true },
      }],
    })} />);
    await expand();
    await userEvent.setup().click(await screen.findByText("Recorded memory tool calls"));
    expect(screen.getByText(/does not prove delivery to a later model request/)).toBeVisible();
    expect(screen.getByText("A bounded memory tool result")).toBeVisible();
    expect(screen.getByText("The tool result is truncated.")).toBeVisible();
  });

  it("includes recorded workflow and delegated executions and offers the full receipt", async () => {
    const open = vi.fn();
    render(<MemoryProvenance
      workflowReceipts={[receipt({ contextBlocks: [], delegations: [receipt()] })]}
      onOpenReceipt={open}
    />);
    await expand();
    expect(await screen.findByText(/delegated execution/)).toBeVisible();
    expect(screen.getByText("Snapshot: concise answers")).toBeVisible();
    await userEvent.setup().click(screen.getByRole("button", { name: "Open full execution receipt" }));
    expect(open).toHaveBeenCalledOnce();
  });

  it("does not reinterpret an unsupported receipt version", async () => {
    render(<MemoryProvenance receipt={receipt({ version: 99 })} />);
    await expand();
    expect(await screen.findByText(/receipt version is not supported/)).toBeVisible();
    expect(screen.queryByText("Snapshot: concise answers")).not.toBeInTheDocument();
  });

  it("reports the focused view's own bound even when individual receipts are complete", async () => {
    render(<MemoryProvenance workflowReceipts={Array.from({ length: 33 }, () => receipt())} />);
    await expand();
    expect(await screen.findByText(/Additional recorded executions are omitted/)).toBeVisible();
    expect(screen.getAllByText("Recorded memory context")).toHaveLength(32);
  });
});
