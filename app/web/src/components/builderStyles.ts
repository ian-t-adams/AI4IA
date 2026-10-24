// Shared controls for the Studio builders.

export const labelStyle: React.CSSProperties = {
  fontSize: "0.875em",
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
  minHeight: 44,
  padding: "9px 16px",
  borderRadius: 8,
  border: "none",
  background: "var(--accent)",
  color: "var(--accent-fg)",
  fontWeight: 600,
  cursor: "pointer",
};

export const secondaryBtn: React.CSSProperties = {
  minHeight: 44,
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
  minHeight: 44,
  padding: "4px 8px",
  borderRadius: 6,
  border: "1px solid var(--border)",
  background: "var(--bg)",
  color: "var(--fg)",
  cursor: "pointer",
  fontSize: "0.85em",
};

export const fieldset: React.CSSProperties = {
  border: 0,
  margin: 0,
  padding: 0,
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
