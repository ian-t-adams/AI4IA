"use client";

import type { ExecutionReceipt } from "@/lib/types";
import { useState } from "react";

function recordedExecutions(receipts: ExecutionReceipt[]) {
  const entries: { receipt: ExecutionReceipt; label: string }[] = [];
  let bounded = false;
  function visit(receipt: ExecutionReceipt, label: string, depth: number) {
    if (entries.length >= 32 || depth > 4) {
      bounded = true;
      return;
    }
    entries.push({ receipt, label });
    if (receipt.version === 1) {
      for (const child of receipt.delegations ?? []) {
        visit(child, `${label} / delegated execution`, depth + 1);
      }
    }
  }
  receipts.forEach((receipt, index) => visit(receipt, `Recorded execution ${index + 1}`, 0));
  return { entries, bounded };
}

export function MemoryProvenance({
  receipt,
  workflowReceipts,
  onInspectMemory,
  onOpenReceipt,
}: {
  receipt?: ExecutionReceipt | null;
  workflowReceipts?: ExecutionReceipt[] | null;
  onInspectMemory?: (memoryId: string | null) => void;
  onOpenReceipt?: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const { entries: executions, bounded } = recordedExecutions([
    ...(receipt ? [receipt] : []),
    ...(workflowReceipts ?? []),
  ]);
  return (
    <details className="activity activity-trace" onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary>Memories supplied{executions.length ? "" : " - unrecorded"}</summary>
      {expanded ? <div className="activity-rows">
        <p className="inspector-note">
          Context supplied to a model is not proof it influenced a sentence, and is not hidden reasoning.
          These are bounded receipt snapshots, not reconstructions from your current memories.
          Disabling or deleting memory does not erase past messages or receipts.
        </p>
        {!executions.length ? (
          <p>Memory provenance was not recorded for this answer. It has not been backfilled.</p>
        ) : executions.map(({ receipt: execution, label }, index) => {
          if (execution.version !== 1) {
            return <p key={index}>{label}: this receipt version is not supported by this memory view.</p>;
          }
          const blocks = (execution.contextBlocks ?? []).filter((block) => block.kind === "memory");
          const supplied = blocks.filter((block) => block.admitted);
          const recalls = (execution.toolCalls ?? []).filter((call) => call.tool === "recall_memory");
          return (
            <div key={index}>
              {executions.length > 1 ? <p>{label}{execution.runtime.agent ? ` (${execution.runtime.agent})` : ""}</p> : null}
              {!supplied.length ? <p>No automatically recalled memory is recorded as supplied in this receipt.</p> : null}
              {blocks.some((block) => !block.admitted) ? <p>Built memory context was withheld from the model.</p> : null}
              {supplied.map((block, blockIndex) => (
                <div key={blockIndex}>
                  <p>Recorded memory context</p>
                  {block.content?.text ? (
                    <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{block.content.text}</pre>
                  ) : <p>The context body was not retained.</p>}
                  {block.content?.truncated ? <p>The context excerpt is truncated.</p> : null}
                  {(block.sources ?? []).length ? (
                    <ul>
                      {(block.sources ?? []).map((source, sourceIndex) => (
                        <li key={`${source.id}-${sourceIndex}`}>
                          {onInspectMemory ? (
                            <button type="button" onClick={() => onInspectMemory(source.id)}>
                              Inspect memory {source.id}
                            </button>
                          ) : <span>Memory {source.id}</span>}
                          {source.version ? ` (recorded version ${source.version})` : ""}
                        </li>
                      ))}
                    </ul>
                  ) : <p>Item references were not retained; no links have been inferred.</p>}
                  {(block.sourceCount ?? 0) > (block.sources ?? []).length ? (
                    <p>Additional source references were omitted by the receipt limit.</p>
                  ) : null}
                </div>
              ))}
              {recalls.length ? (
                <details>
                  <summary>Recorded memory tool calls</summary>
                  <p>A tool return alone does not prove delivery to a later model request. See the full receipt for retained request evidence.</p>
                  {recalls.map((call, callIndex) => (
                    <div key={callIndex}>
                      <p>{call.outcome}</p>
                      {call.result?.text ? (
                        <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{call.result.text}</pre>
                      ) : <p>No tool result body is recorded.</p>}
                      {call.result?.truncated ? <p>The tool result is truncated.</p> : null}
                    </div>
                  ))}
                </details>
              ) : null}
              {execution.partial || execution.truncated ? (
                <p>This receipt is partial or bounded; missing details do not prove memory was unused.</p>
              ) : null}
            </div>
          );
        })}
        {bounded ? <p>Additional recorded executions are omitted from this focused view. Open the full receipt for the retained evidence.</p> : null}
        {onInspectMemory ? <button type="button" onClick={() => onInspectMemory(null)}>Manage memories</button> : null}
        {executions.length && onOpenReceipt ? <button type="button" onClick={onOpenReceipt}>Open full execution receipt</button> : null}
      </div> : null}
    </details>
  );
}
