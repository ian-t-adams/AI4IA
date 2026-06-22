import { NextRequest, NextResponse } from "next/server";

// Per-request, nonce-based Content-Security-Policy for the App Router.
//
// Implemented as Next 16's `proxy` convention (the successor to the deprecated
// `middleware` file/export; the runtime behavior is identical). This is the
// app-aware follow-up to the conservative CSP baseline shipped in
// next.config.mjs (#83). It adds the high-value, previously-missing piece: a
// strict `script-src` that only permits scripts carrying a fresh, unguessable
// per-request nonce (plus `'strict-dynamic'`, so those trusted scripts may load
// their own bundle chunks). That neutralizes injected-<script> XSS without an
// origin allowlist to maintain.
//
// How the nonce reaches Next's own scripts: we set the CSP on the *request*
// headers via `NextResponse.next({ request })`. During SSR, Next parses that
// header, extracts `'nonce-<value>'`, and stamps it onto every framework script,
// page bundle, and inline style/script it emits — so no manual wiring in the
// layout is needed. We also expose it as `x-nonce` for any future inline
// <Script nonce=...> needs.
//
// Deliberate, documented relaxations (see the PR body for the full rationale):
//   * `style-src 'self' 'unsafe-inline'` — the UI renders inline `style={{}}`
//     attributes throughout; style *attributes* can't be covered by a nonce or
//     hash, only by `'unsafe-inline'`. Style injection is far lower severity
//     than script injection, which stays locked down.
//   * No `default-src` / `connect-src` / `img-src` restriction — the Voice Live
//     feature opens a WebSocket *directly* to the API's external ingress
//     (a cross-origin, env-configured `wss://` origin the Next proxy can't
//     proxy), and generated backgrounds use `data:`/`blob:` images. Constraining
//     these would break those at runtime, which is exactly what the #83 baseline
//     avoided. We keep that posture and only tighten scripts here.
//
// Using a nonce forces dynamic rendering; the rendered routes (`/`, `/admin`)
// are already `export const dynamic = "force-dynamic"`, so this is a no-op for
// them.

export function proxy(request: NextRequest): NextResponse {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  // React uses `eval` for richer error overlays in `next dev`; production builds
  // need neither `unsafe-eval` nor `unsafe-inline` for scripts.
  const isDev = process.env.NODE_ENV === "development";

  const cspHeader = `
    script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${isDev ? " 'unsafe-eval'" : ""};
    style-src 'self' 'unsafe-inline';
    base-uri 'self';
    object-src 'none';
    frame-ancestors 'none';
    form-action 'self';
  `
    .replace(/\s{2,}/g, " ")
    .trim();

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", cspHeader);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", cspHeader);
  return response;
}

export const config = {
  matcher: [
    // Run on document routes only. Skip API routes (the same-origin proxy and
    // its streamed responses), Next's static/image assets, and the favicon —
    // none of which render HTML that needs a script nonce. Also skip link
    // prefetches, whose cached response would otherwise pin a stale nonce.
    {
      source: "/((?!api|_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
