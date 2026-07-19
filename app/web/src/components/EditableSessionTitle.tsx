"use client";

import { useEffect, useId, useRef, useState } from "react";

import { apiErrorDetail } from "@/lib/api";

export function EditableSessionTitle({
  title,
  onSave,
  disabled = false,
  disabledReasonId,
  compact = false,
  onOpen,
  current = false,
}: {
  title: string;
  onSave: (title: string) => Promise<void>;
  disabled?: boolean;
  /** Id of a visible element (elsewhere on the page) describing why disabled, wired via aria-describedby. */
  disabledReasonId?: string;
  compact?: boolean;
  onOpen?: () => void;
  current?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(title);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef(false);
  const inputId = useId();

  useEffect(() => {
    if (editing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    } else if (restoreFocusRef.current) {
      restoreFocusRef.current = false;
      triggerRef.current?.focus();
    }
  }, [editing]);

  const finish = (restoreFocus = true) => {
    restoreFocusRef.current = restoreFocus;
    setEditing(false);
  };

  const begin = () => {
    setValue(title);
    setStatus("");
    setEditing(true);
  };

  const cancel = () => {
    setValue(title);
    setStatus("Rename cancelled");
    finish();
  };

  const save = async (restoreFocus = true) => {
    if (saving) return;
    const next = value.trim();
    if (next === title) {
      finish(restoreFocus);
      return;
    }
    if (!next || next.length > 120) {
      setStatus(
        !next
          ? "Conversation title cannot be empty."
          : "Conversation title must be 120 characters or fewer.",
      );
      inputRef.current?.focus();
      return;
    }
    setSaving(true);
    setStatus("Saving title");
    try {
      await onSave(next);
      setStatus("Title saved");
      finish(restoreFocus);
    } catch (reason) {
      setStatus(apiErrorDetail(reason));
      inputRef.current?.focus();
    } finally {
      setSaving(false);
    }
  };

  if (editing) {
    return (
      <span className={`editable-session-title editing${compact ? " compact" : ""}`}>
        <label className="visually-hidden" htmlFor={inputId}>
          Conversation title
        </label>
        <input
          ref={inputRef}
          id={inputId}
          value={value}
          maxLength={120}
          disabled={saving}
          onChange={(event) => setValue(event.target.value)}
          onBlur={() => void save(false)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              event.stopPropagation();
              void save();
            } else if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              cancel();
            }
          }}
        />
        <button
          type="button"
          disabled={saving}
          onPointerDown={(event) => event.preventDefault()}
          onClick={() => void save()}
        >
          {saving ? "Saving..." : "Save"}
        </button>
        <button
          type="button"
          disabled={saving}
          onPointerDown={(event) => event.preventDefault()}
          onClick={cancel}
        >
          Cancel
        </button>
        <span className="visually-hidden" role="status" aria-live="polite">
          {status}
        </span>
      </span>
    );
  }

  return (
    <span className={`editable-session-title${compact ? " compact" : ""}`}>
      {onOpen ? (
        <button
          type="button"
          className="editable-session-title-text"
          aria-disabled={disabled || undefined}
          aria-describedby={disabled && disabledReasonId ? disabledReasonId : undefined}
          aria-current={current ? "true" : undefined}
          onClick={() => {
            if (disabled) return;
            onOpen();
          }}
          onKeyDown={(event) => {
            if (event.key === "F2") {
              event.preventDefault();
              if (disabled) return;
              begin();
            }
          }}
        >
          {title || "Untitled"}
        </button>
      ) : (
        <span className="editable-session-title-text">{title || "Untitled"}</span>
      )}
      <button
        ref={triggerRef}
        type="button"
        className="editable-session-title-trigger"
        aria-disabled={disabled || undefined}
        aria-describedby={disabled && disabledReasonId ? disabledReasonId : undefined}
        onClick={() => {
          if (disabled) return;
          begin();
        }}
        onKeyDown={(event) => {
          if (event.key === "F2") {
            event.preventDefault();
            if (disabled) return;
            begin();
          }
        }}
      >
        Rename
      </button>
      <span className="visually-hidden" role="status" aria-live="polite">
        {status}
      </span>
    </span>
  );
}
