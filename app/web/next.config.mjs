/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  // Lint is run explicitly via `npm run lint` (and in CI) so the production
  // build is deterministic and not blocked by lint-only findings.
  eslint: {
    ignoreDuringBuilds: true,
  },
};

export default nextConfig;
