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
  // No next/image is used in this app (all image render sites use a plain
  // <img> with eslint-disable-next-line @next/next/no-img-element). Setting
  // unoptimized:true disables the built-in image optimizer so that sharp is
  // never invoked at runtime, making its optional-dependency status enforced
  // rather than incidental.
  images: {
    unoptimized: true,
  },
  // Linting runs as a dedicated CI step (`npm run lint`); Next 16 no longer
  // executes ESLint during `next build`, so the former `eslint` config key
  // (ignoreDuringBuilds) is removed — it is unsupported in Next 16.
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
