"use client";

// Compact signed-in account chip + sign-out, shown in the chat header. Renders
// nothing in dev mode (no MSAL), and the early return runs before any hook so it
// is safe to mount outside MsalProvider. The Entra subtree is only reached when
// Entra is enabled, where the app is wrapped in MsalProvider.
import { useMsal } from "@azure/msal-react";

import { isEntraEnabled } from "@/lib/auth";

function EntraUserMenu({ disabled }: { disabled: boolean }) {
  const { instance, accounts } = useMsal();
  const account = instance.getActiveAccount() ?? accounts[0] ?? null;
  if (!account) return null;

  const label = account.name || account.username || "Account";
  const signOut = () => {
    void instance.logoutRedirect().catch(() => {
      /* transient; user can retry */
    });
  };

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <span
        title={account.username}
        style={{
          fontSize: "0.8em",
          color: "var(--fg-muted)",
          maxWidth: 180,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {label}
      </span>
      <button
        type="button"
        onClick={signOut}
        disabled={disabled}
        title={disabled ? "Finish saving the Voice Live transcript first" : undefined}
        style={{
          fontSize: "0.8em",
          padding: "4px 10px",
          borderRadius: 6,
          border: "1px solid var(--border)",
          background: "var(--bg-elevated)",
          color: "var(--fg)",
          cursor: disabled ? "not-allowed" : "pointer",
          opacity: disabled ? 0.5 : 1,
        }}
      >
        Sign out
      </button>
    </div>
  );
}

export function UserMenu({ disabled = false }: { disabled?: boolean }) {
  if (!isEntraEnabled()) return null;
  return <EntraUserMenu disabled={disabled} />;
}
