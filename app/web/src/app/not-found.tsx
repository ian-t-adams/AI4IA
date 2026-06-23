import Link from "next/link";

// Custom 404. Beyond branding, this exists so the not-found route is rendered
// dynamically: the nonce-based CSP (src/proxy.ts) is minted per request, so
// a *statically* prerendered page would ship framework scripts without the
// request's nonce and have them blocked by `script-src`. Forcing dynamic
// rendering lets Next stamp the per-request nonce here too, keeping the page
// fully hydrated and CSP-clean.
export const dynamic = "force-dynamic";

export default function NotFound() {
  return (
    <main
      id="main"
      style={{
        minHeight: "100vh",
        display: "grid",
        placeItems: "center",
        padding: "2rem",
        background: "var(--bg)",
        color: "var(--fg)",
      }}
    >
      <section
        style={{
          maxWidth: "32rem",
          textAlign: "center",
          padding: "1.5rem",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius)",
          background: "var(--bg-elevated)",
        }}
      >
        <p style={{ margin: "0 0 0.5rem", color: "var(--fg-muted)", fontWeight: 700 }}>
          404
        </p>
        <h1 style={{ margin: "0 0 0.75rem", fontSize: "1.5rem" }}>
          That page doesn&apos;t exist.
        </h1>
        <p style={{ margin: "0 0 1.25rem", color: "var(--fg-muted)" }}>
          The link may be broken or the page may have moved.
        </p>
        <Link
          href="/"
          style={{
            display: "inline-block",
            borderRadius: "999px",
            padding: "0.65rem 1.1rem",
            color: "var(--accent-fg)",
            background: "var(--accent)",
            textDecoration: "none",
          }}
        >
          Back to chat
        </Link>
      </section>
    </main>
  );
}
