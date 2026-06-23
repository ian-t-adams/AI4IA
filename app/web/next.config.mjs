// Baseline security response headers applied to every route.
//
// The Content-Security-Policy is NOT set here: it is now issued per-request by
// `src/proxy.ts`, which mints a fresh nonce and ships a strict, nonce-based
// `script-src` (plus the baseline base-uri / object-src / frame-ancestors /
// form-action directives that previously lived here). A static header can't
// carry a per-request nonce, and emitting CSP from both places would produce two
// conflicting `Content-Security-Policy` headers. The static, request-independent
// headers below still apply to *every* route (including /api and static assets,
// which the CSP proxy intentionally skips).
const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  // Keep microphone + camera for self (voice / multimodal AV); deny the rest.
  {
    key: "Permissions-Policy",
    value: "camera=(self), microphone=(self), geolocation=()",
  },
  // Activates only over HTTPS; ignored on http/localhost. No preload (avoids the
  // hard-to-reverse preload-list commitment).
  {
    key: "Strict-Transport-Security",
    value: "max-age=63072000; includeSubDomains",
  },
];

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  // Lint is run explicitly via `npm run lint` (and in CI) so the production
  // build is deterministic and not blocked by lint-only findings.
  eslint: {
    ignoreDuringBuilds: true,
  },
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
