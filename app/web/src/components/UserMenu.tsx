"use client";

// Compact signed-in account chip + sign-out, shown in the chat header. Renders
// nothing in dev mode (no MSAL), and the early return runs before any hook so it
// is safe to mount outside MsalProvider. The Entra subtree is only reached when
// Entra is enabled, where the app is wrapped in MsalProvider.
import { useMsal } from "@azure/msal-react";

import { isEntraEnabled } from "@/lib/auth";

function EntraUserMenu({
  onBeforeSignOut,
}: {
  onBeforeSignOut?: () => boolean | void;
}) {
  const { instance, accounts } = useMsal();
  const account = instance.getActiveAccount() ?? accounts[0] ?? null;
  if (!account) return null;

  const label = account.name || account.username || "Account";
  const signOut = () => {
    if (onBeforeSignOut?.() === false) return;
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
      >
        <span aria-hidden="true">↪</span>
        <span>Sign out</span>
      </button>
    </div>
  );
}

export function UserMenu({
  onBeforeSignOut,
}: {
  onBeforeSignOut?: () => boolean | void;
}) {
  if (!isEntraEnabled()) return null;
  return <EntraUserMenu onBeforeSignOut={onBeforeSignOut} />;
}
