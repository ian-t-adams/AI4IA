"use client";

// Header entry point to the admin dashboard. Self-fetches `whoami` and renders a
// link to /admin ONLY for admins — a cosmetic hide. The server's `require_admin`
// is the real boundary, so a non-admin who navigates to /admin directly still
// gets a forbidden view (and the API still 403s). Renders nothing until the
// whoami check resolves, and nothing on error, so it never flashes for non-admins.
import { useEffect, useState } from "react";

import { canShowAdmin, fetchWhoAmI } from "@/lib/admin";

export function AdminLink({ disabled = false }: { disabled?: boolean }) {
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
      aria-disabled={disabled}
      tabIndex={disabled ? -1 : undefined}
      title={disabled ? "Finish saving the Voice Live transcript first" : undefined}
      onClick={disabled ? (event) => event.preventDefault() : undefined}
      style={{
        fontSize: "0.8em",
        padding: "4px 10px",
        borderRadius: 6,
        border: "1px solid var(--border)",
        background: "var(--bg-elevated)",
        color: "var(--fg)",
        textDecoration: "none",
        whiteSpace: "nowrap",
        opacity: disabled ? 0.5 : 1,
        cursor: disabled ? "not-allowed" : "pointer",
      }}
    >
      Admin
    </a>
  );
}
