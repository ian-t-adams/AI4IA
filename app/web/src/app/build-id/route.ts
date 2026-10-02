// The web build this server is running, for stale-tab detection
// (src/lib/webBuild.ts). Public and content-free: the same identifier is already
// inlined into the browser bundle. Deliberately outside /api/*, which is proxied
// to FastAPI, and never cached, so a poll always reaches the serving build.
export const dynamic = "force-dynamic";

export function GET(): Response {
  return Response.json(
    { buildId: process.env.AI4IA_WEB_BUILD_ID ?? null },
    { headers: { "Cache-Control": "no-store" } },
  );
}
