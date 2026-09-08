"use client";

import { useEffect, useId, useRef, useSyncExternalStore } from "react";
import type { MemoryPreferenceState } from "@/lib/memoryPreferenceState";
import { useMemoryPreferenceStore } from "./MemoryPreferenceProvider";

const unavailable: MemoryPreferenceState = {
  preference: null, phase: "error", error: "Sign in to manage your memory preference.",
};
const noSubscription = () => () => {};
const unavailableSnapshot = () => unavailable;

export function MemoryPreferenceControl({ onChanged }: { onChanged?: () => void }) {
  const id = useId();
  const store = useMemoryPreferenceStore();
  const bindingRef = useRef({ active: false });
  const { preference, phase, error } = useSyncExternalStore(
    store?.subscribe ?? noSubscription,
    store?.getSnapshot ?? unavailableSnapshot,
    unavailableSnapshot,
  );

  useEffect(() => {
    const binding = { active: true };
    bindingRef.current = binding;
    void store?.refresh();
    return () => { binding.active = false; };
  }, [store]);

  async function change(enabled: boolean) {
    const binding = bindingRef.current;
    await store?.change(enabled);
    if (binding.active) onChanged?.();
  }

  return (
    <div aria-busy={phase === "loading" || phase === "saving"}>
      <label htmlFor={id} className="inspector-actions">
        <input
          id={id}
          type="checkbox"
          role="switch"
          checked={preference?.automaticMemoryEnabled ?? true}
          disabled={phase !== "ready"}
          aria-describedby={`${id}-help ${id}-status`}
          onChange={(event) => void change(event.target.checked)}
        />
        Automatic memory
      </label>
      <p id={`${id}-help`} className="inspector-note">
        On by default across your conversations and workflows. Off stops automatic recall,
        saving, and model memory tools. You can still manage saved memories below.
        This is not tool consent and does not erase memories, past messages, or receipts.
      </p>
      <p id={`${id}-status`} role="status" className="inspector-note">
        {phase === "loading" ? "Loading memory preference..."
          : phase === "saving" ? "Saving memory preference..."
            : phase === "error" ? "Current setting is unconfirmed. Reload before changing it."
              : `Automatic memory is ${preference?.automaticMemoryEnabled ? "on" : "off"}.`}
      </p>
      {error ? (
        <div className="inspector-error" role="alert">
          {error} {preference ? "Showing the last confirmed setting." : "The default has not been confirmed."}
          <button type="button" disabled={!store || phase === "saving" || phase === "loading"}
            onClick={() => void store?.refresh()}>Reload memory preference</button>
        </div>
      ) : null}
    </div>
  );
}
