// Shared controls for the Studio builders.

export const labelStyle: React.CSSProperties = {
  fontSize: "var(--control-font-size)",
  fontWeight: 500,
  color: "var(--fg)",
  marginBottom: "var(--space-2)",
  display: "block",
};

export const inputStyle: React.CSSProperties = {
  width: "100%",
  minHeight: "var(--control-height)",
  padding: "var(--control-padding)",
  borderRadius: "var(--control-radius)",
  border: "1px solid var(--border)",
  background: "var(--field-bg)",
  color: "var(--fg)",
  font: "inherit",
};

const buttonStyle: React.CSSProperties = {
  minHeight: "var(--control-height)",
  padding: "var(--control-padding)",
  borderRadius: "var(--control-radius)",
  border: "1px solid var(--border)",
  font: "inherit",
  fontSize: "var(--control-font-size)",
  fontWeight: 600,
  lineHeight: 1.3,
  cursor: "pointer",
};

export const primaryBtn: React.CSSProperties = {
  ...buttonStyle,
  borderColor: "var(--accent)",
  background: "var(--accent)",
  color: "var(--accent-fg)",
};

export const secondaryBtn: React.CSSProperties = {
  ...buttonStyle,
  background: "var(--bg-elevated)",
  color: "var(--fg)",
};

// Compact variant of secondaryBtn used for inline row actions (e.g. workflow
// step "Remove"/"Move" controls) where a full-size button would be too big.
export const ghostBtn: React.CSSProperties = {
  ...buttonStyle,
  padding: "var(--space-2)",
  background: "transparent",
  color: "var(--fg)",
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
