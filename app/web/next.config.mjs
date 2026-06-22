// Baseline security response headers applied to every route.
//
// Deliberately conservative on CSP: we do NOT set a restrictive default-src /
// script-src here. This app serves Next.js inline hydration scripts and opens
// websockets/WebRTC to the voice + multimodal-AV backends; a strict script/
// connect policy without nonce wiring would break those at runtime. Instead we
// ship the high-value directives that cost nothing functionally — clickjacking
// (frame-ancestors), base-tag injection (base-uri), plugin (object-src), and
// form-hijacking (form-action) protection. A full nonce-based CSP is a separate,
// app-aware change.
const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  {
    key: "Content-Security-Policy",
    value:
      "base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'",
  },
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
