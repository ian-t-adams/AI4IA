// Shared style constants for the "builder" surfaces (AgentBuilder,
// WorkflowBuilder, McpServerBuilder). These were previously copy-pasted
// verbatim (or near-verbatim) into each file; consolidating them here means a
// visual tweak only needs to happen once, and keeps each builder file focused
// on its own behavior instead of restating the same CSSProperties objects.
//
// `fieldset` unifies two slightly different variants (8px/gap:4 in
// AgentBuilder vs 10px/gap:8 in McpServerBuilder) on the slightly more
// spacious McpServerBuilder spacing — a minor, intentional cosmetic
// harmonization, not a behavior change.

export const labelStyle: React.CSSProperties = {
  fontSize: "0.8em",
  color: "var(--fg-muted)",
  marginBottom: 4,
  display: "block",
};

export const inputStyle: React.CSSProperties = {
  width: "100%",
  padding: "8px 10px",
  borderRadius: 8,
  border: "1px solid var(--border)",
  background: "var(--bg)",
  color: "var(--fg)",
  font: "inherit",
};

export const primaryBtn: React.CSSProperties = {
  padding: "9px 16px",
  borderRadius: 8,
  border: "none",
  background: "var(--accent)",
  color: "var(--accent-fg)",
  fontWeight: 600,
  cursor: "pointer",
};

export const secondaryBtn: React.CSSProperties = {
  padding: "9px 16px",
  borderRadius: 8,
  border: "1px solid var(--border)",
  background: "var(--bg)",
  color: "var(--fg)",
  fontWeight: 600,
  cursor: "pointer",
};

// Compact variant of secondaryBtn used for inline row actions (e.g. workflow
// step "Remove"/"Move" controls) where a full-size button would be too big.
export const ghostBtn: React.CSSProperties = {
  padding: "4px 8px",
  borderRadius: 6,
  border: "1px solid var(--border)",
  background: "var(--bg)",
  color: "var(--fg)",
  cursor: "pointer",
  fontSize: "0.85em",
};

export const iconBtn: React.CSSProperties = {
  border: "none",
  background: "transparent",
  color: "var(--fg-muted)",
  padding: "4px 6px",
  cursor: "pointer",
};

export const fieldset: React.CSSProperties = {
  border: "1px solid var(--border)",
  borderRadius: 8,
  margin: 0,
  padding: "10px 12px",
  display: "flex",
  flexDirection: "column",
  gap: 8,
};

export const checkRow: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  fontSize: "0.9em",
};
