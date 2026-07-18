"use client";

// Header entry point to the admin dashboard. Self-fetches `whoami` and renders a
// link to /admin ONLY for admins — a cosmetic hide. The server's `require_admin`
// is the real boundary, so a non-admin who navigates to /admin directly still
// gets a forbidden view (and the API still 403s). Renders nothing until the
// whoami check resolves, and nothing on error, so it never flashes for non-admins.
import { useEffect, useState } from "react";

import { canShowAdmin, fetchWhoAmI } from "@/lib/admin";

export function AdminLink({
  disabled = false,
  disabledReasonId,
}: {
  disabled?: boolean;
  /** Id of a visible element (elsewhere on the page) describing why disabled, wired via aria-describedby. */
  disabledReasonId?: string;
}) {
  const [isAdmin, setIsAdmin] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchWhoAmI()
      .then((who) => {
        if (!cancelled) setIsAdmin(canShowAdmin(who));
      })
      .catch(() => {
        if (!cancelled) setIsAdmin(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!isAdmin) return null;

  return (
    <a
      href={disabled ? undefined : "/admin"}
      className="sidebar-utility-action"
      aria-disabled={disabled}
      tabIndex={disabled ? 0 : undefined}
      aria-describedby={disabled && disabledReasonId ? disabledReasonId : undefined}
      onClick={disabled ? (event) => event.preventDefault() : undefined}
    >
      <span aria-hidden="true">◆</span>
      <span>Admin</span>
    </a>
  );
}
