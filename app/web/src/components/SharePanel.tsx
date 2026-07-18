"use client";

// Document-level sharing dialog. Lets the owner of a library document
// choose its visibility (private / shared with specific people / anyone in the
// tenant) and, for "shared", manage the grantee email list. Launched as its own
// modal from LibraryPanel; owner-only on the server, inert unless the document
// library flag is on.
//
// Grants are keyed on EMAIL. Changes are staged locally and applied with a single
// Save (the API's PUT replaces visibility + the whole grant list). Annotations and
// saved memories deliberately do NOT travel with a shared document.
import { useCallback, useEffect, useState } from "react";

import { getDocumentShares, setDocumentShares } from "@/lib/api";
import type { ShareVisibility } from "@/lib/library";
import { useModalFocus } from "./useModalFocus";

interface SharePanelProps {
  documentId: string;
  filename: string;
  onClose: () => void;
  // Notifies the parent of the document's new visibility so the list badge can
  // update without a full refetch.
  onChanged?: (visibility: ShareVisibility) => void;
}

const VISIBILITY_OPTIONS: { value: ShareVisibility; label: string; hint: string }[] = [
  { value: "private", label: "Private", hint: "Only you can access this document." },
  {
    value: "shared",
    label: "Specific people",
    hint: "You plus the people you list below (by email).",
  },
  {
    value: "public",
    label: "Anyone in your organization",
    hint: "Any signed-in user in your tenant can open it. Not listed in their library.",
  },
];

// Light client-side check mirroring the server: a single-token address with a
// local part and a dotted domain. The server is the real authority (422), this
// just catches obvious typos before a round trip.
function looksLikeEmail(value: string): boolean {
  const v = value.trim();
  if (!v || v.includes(" ") || (v.match(/@/g) || []).length !== 1) return false;
  const [local, domain] = v.split("@");
  return (
    local.length > 0 &&
    domain.includes(".") &&
    !domain.startsWith(".") &&
    !domain.endsWith(".")
  );
}

export default function SharePanel({
  documentId,
  filename,
  onClose,
  onChanged,
}: SharePanelProps) {
  const modal = useModalFocus(onClose);
  const [visibility, setVisibility] = useState<ShareVisibility>("private");
  const [grantees, setGrantees] = useState<string[]>([]);
  const [draftEmail, setDraftEmail] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const state = await getDocumentShares(documentId);
        if (!active) return;
        setVisibility(state.visibility);
        setGrantees(state.grantees);
      } catch (e) {
        if (active) setError(e instanceof Error ? e.message : "Failed to load sharing");
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [documentId]);

  const addGrantee = useCallback(() => {
    const email = draftEmail.trim().toLowerCase();
    if (!email) return;
    if (!looksLikeEmail(email)) {
      setError(`"${draftEmail.trim()}" doesn't look like an email address.`);
      return;
    }
    setError(null);
    setSaved(false);
    setGrantees((prev) => (prev.includes(email) ? prev : [...prev, email]));
    setDraftEmail("");
  }, [draftEmail]);

  const removeGrantee = useCallback((email: string) => {
    setSaved(false);
    setGrantees((prev) => prev.filter((g) => g !== email));
  }, []);

  const handleSave = useCallback(async () => {
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      const state = await setDocumentShares(
        documentId,
        visibility,
        visibility === "shared" ? grantees : [],
      );
      setVisibility(state.visibility);
      setGrantees(state.grantees);
      setSaved(true);
      onChanged?.(state.visibility);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save sharing");
    } finally {
      setSaving(false);
    }
  }, [documentId, grantees, onChanged, saving, visibility]);

  return (
    <div
      ref={modal.ref}
      onKeyDown={modal.onKeyDown}
      role="dialog"
      aria-label={`Share ${filename}`}
      aria-modal="true"
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.45)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 60,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--bg-elevated)",
          color: "var(--fg)",
          width: "min(520px, 94vw)",
          borderRadius: "var(--radius)",
          border: "1px solid var(--border)",
          padding: 24,
          display: "flex",
          flexDirection: "column",
          gap: 16,
          maxHeight: "90vh",
          overflowY: "auto",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
          <h2 style={{ margin: 0, fontSize: "1.1em" }}>
            🔗 Share
            <span
              style={{
                display: "block",
                fontSize: "0.7em",
                fontWeight: 400,
                color: "var(--fg-muted)",
                marginTop: 2,
              }}
            >
              {filename}
            </span>
          </h2>
          <button
            onClick={onClose}
            aria-label="Close sharing"
            style={{
              border: "none",
              background: "transparent",
              color: "var(--fg)",
              fontSize: "1.2em",
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        </div>

        <p style={{ margin: 0, fontSize: "0.8em", color: "var(--fg-muted)" }}>
          People you share with can read, search, and run code over this document
          through their own assistant. Your private notes and saved memories are
          never shared.
        </p>

        {loading ? (
          <p style={{ margin: 0, fontSize: "0.85em", color: "var(--fg-muted)" }}>
            Loading…
          </p>
        ) : (
          <>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {VISIBILITY_OPTIONS.map((opt) => (
                <label
                  key={opt.value}
                  style={{
                    display: "flex",
                    gap: 8,
                    alignItems: "flex-start",
                    fontSize: "0.9em",
                    cursor: "pointer",
                  }}
                >
                  <input
                    type="radio"
                    name="visibility"
                    checked={visibility === opt.value}
                    onChange={() => {
                      setVisibility(opt.value);
                      setSaved(false);
                    }}
                    style={{ marginTop: 3 }}
                  />
                  <span>
                    <span style={{ fontWeight: 600 }}>{opt.label}</span>
                    <span
                      style={{
                        display: "block",
                        fontSize: "0.8em",
                        color: "var(--fg-muted)",
                      }}
                    >
                      {opt.hint}
                    </span>
                  </span>
                </label>
              ))}
            </div>

            {visibility !== "shared" && grantees.length > 0 && (
              <p
                role="status"
                style={{
                  margin: 0,
                  fontSize: "0.8em",
                  color: "var(--danger, #dc2626)",
                }}
              >
                Saving now will remove access for the {grantees.length}{" "}
                {grantees.length === 1 ? "person" : "people"} currently shared
                with — switch back to &ldquo;Specific people&rdquo; to keep them.
              </p>
            )}

            {visibility === "shared" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <div style={{ display: "flex", gap: 8 }}>
                  <input
                    value={draftEmail}
                    onChange={(e) => setDraftEmail(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        addGrantee();
                      }
                    }}
                    placeholder="colleague@example.com"
                    style={{
                      flex: 1,
                      padding: "8px 10px",
                      borderRadius: 8,
                      border: "1px solid var(--border)",
                      background: "var(--bg)",
                      color: "var(--fg)",
                      fontSize: "0.85em",
                    }}
                  />
                  <button
                    onClick={addGrantee}
                    disabled={draftEmail.trim().length === 0}
                    style={{
                      padding: "6px 14px",
                      borderRadius: 8,
                      border: "none",
                      background:
                        draftEmail.trim().length === 0
                          ? "var(--border)"
                          : "var(--accent)",
                      color: "#fff",
                      fontSize: "0.85em",
                      cursor:
                        draftEmail.trim().length === 0 ? "default" : "pointer",
                    }}
                  >
                    Add
                  </button>
                </div>
                {grantees.length === 0 ? (
                  <span style={{ fontSize: "0.8em", color: "var(--fg-muted)" }}>
                    No one yet — add an email above.
                  </span>
                ) : (
                  <div
                    style={{ display: "flex", flexWrap: "wrap", gap: 6 }}
                    aria-label="People with access"
                  >
                    {grantees.map((email) => (
                      <span
                        key={email}
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 6,
                          padding: "4px 8px",
                          borderRadius: 999,
                          border: "1px solid var(--border)",
                          background: "var(--bg)",
                          fontSize: "0.8em",
                        }}
                      >
                        {email}
                        <button
                          onClick={() => removeGrantee(email)}
                          aria-label={`Remove ${email}`}
                          title={`Remove ${email}`}
                          style={{
                            border: "none",
                            background: "transparent",
                            color: "var(--fg-muted)",
                            fontSize: "0.9em",
                            cursor: "pointer",
                            lineHeight: 1,
                          }}
                        >
                          ✕
                        </button>
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}

            {error && (
              <p style={{ margin: 0, fontSize: "0.8em", color: "#dc2626" }}>
                {error}
              </p>
            )}

            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <button
                onClick={() => void handleSave()}
                disabled={saving}
                style={{
                  padding: "6px 16px",
                  borderRadius: 8,
                  border: "none",
                  background: saving ? "var(--border)" : "var(--accent)",
                  color: "#fff",
                  fontSize: "0.85em",
                  cursor: saving ? "default" : "pointer",
                }}
              >
                {saving ? "Saving…" : "Save"}
              </button>
              {saved && (
                <span style={{ fontSize: "0.8em", color: "#15803d" }}>
                  Sharing updated ✓
                </span>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
