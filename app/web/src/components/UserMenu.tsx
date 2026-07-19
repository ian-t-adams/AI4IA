"use client";

// Compact signed-in account chip + sign-out, shown in the chat header. Renders
// nothing in dev mode (no MSAL), and the early return runs before any hook so it
// is safe to mount outside MsalProvider. The Entra subtree is only reached when
// Entra is enabled, where the app is wrapped in MsalProvider.
import { useMsal } from "@azure/msal-react";

import { isEntraEnabled } from "@/lib/auth";

function EntraUserMenu({
  disabled,
  disabledReasonId,
}: {
  disabled: boolean;
  disabledReasonId?: string;
}) {
  const { instance, accounts } = useMsal();
  const account = instance.getActiveAccount() ?? accounts[0] ?? null;
  if (!account) return null;

  const label = account.name || account.username || "Account";
  const signOut = () => {
    if (disabled) return;
    void instance.logoutRedirect().catch(() => {
      /* transient; user can retry */
    });
  };

  return (
    <div className="sidebar-account">
      <span
        title={account.username}
        className="sidebar-account-label"
      >
        {label}
      </span>
      <button
        type="button"
        className="sidebar-utility-action"
        onClick={signOut}
        aria-disabled={disabled || undefined}
        aria-describedby={disabled && disabledReasonId ? disabledReasonId : undefined}
      >
        <span aria-hidden="true">↪</span>
        <span>Sign out</span>
      </button>
    </div>
  );
}

export function UserMenu({
  disabled = false,
  disabledReasonId,
}: {
  disabled?: boolean;
  disabledReasonId?: string;
}) {
  if (!isEntraEnabled()) return null;
  return <EntraUserMenu disabled={disabled} disabledReasonId={disabledReasonId} />;
}
