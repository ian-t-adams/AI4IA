"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  getMemoryPreference,
  updateMemoryPreference,
  type MemoryPreference,
} from "@/lib/inspector";

type Phase = "loading" | "ready" | "saving" | "error";

export function MemoryPreferenceControl({ onChanged }: { onChanged?: () => void }) {
  const id = useId();
  const generation = useRef(0);
  const [preference, setPreference] = useState<MemoryPreference | null>(null);
  const [phase, setPhase] = useState<Phase>("loading");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    const current = ++generation.current;
    return getMemoryPreference().then(
      (value) => {
        if (generation.current !== current) return;
        setPreference(value);
        setPhase("ready");
      },
      (reason: unknown) => {
        if (generation.current !== current) return;
        setError(reason instanceof Error ? reason.message : "Memory preference is unavailable.");
        setPhase("error");
      },
    );
  }, []);

  useEffect(() => {
    void load();
    return () => { generation.current += 1; };
  }, [load]);

  async function change(enabled: boolean) {
    if (!preference || phase !== "ready") return;
    const previous = preference;
    const current = ++generation.current;
    setPreference({ ...previous, automaticMemoryEnabled: enabled });
    setPhase("saving");
    setError(null);
    try {
      const value = await updateMemoryPreference(enabled, previous.etag);
      if (generation.current !== current) return;
      setPreference(value);
      setPhase("ready");
      onChanged?.();
    } catch (reason) {
      if (generation.current !== current) return;
      setPreference(previous);
      setError(reason instanceof Error ? reason.message : "The memory preference could not be saved.");
      setPhase("error");
    }
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
          <button type="button" onClick={() => {
            setPhase("loading");
            setError(null);
            void load();
          }}>Reload memory preference</button>
        </div>
      ) : null}
    </div>
  );
}
