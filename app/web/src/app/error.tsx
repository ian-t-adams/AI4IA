"use client";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
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
        role="alert"
        aria-labelledby="app-error-title"
        style={{
          maxWidth: "40rem",
          padding: "1.5rem",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius)",
          background: "var(--bg-elevated)",
          boxShadow: "0 12px 30px rgb(0 0 0 / 0.12)",
        }}
      >
        <p style={{ margin: "0 0 0.5rem", color: "var(--danger)", fontWeight: 700 }}>
          The app hit a client-side error.
        </p>
        <h1 id="app-error-title" style={{ margin: "0 0 0.75rem", fontSize: "1.5rem" }}>
          Something went wrong in this chat surface.
        </h1>
        <p style={{ margin: "0 0 1rem", color: "var(--fg-muted)" }}>
          Try reloading the current route. If it keeps happening, capture the browser console
          and API correlation id before retrying the action.
        </p>
        <button
          type="button"
          onClick={reset}
          style={{
            border: 0,
            borderRadius: "999px",
            padding: "0.65rem 1rem",
            color: "var(--accent-fg)",
            background: "var(--accent)",
          }}
        >
          Retry
        </button>
        {error.digest ? (
          <p style={{ margin: "1rem 0 0", color: "var(--fg-muted)", fontSize: "0.85rem" }}>
            Digest: {error.digest}
          </p>
        ) : null}
      </section>
    </main>
  );
}
