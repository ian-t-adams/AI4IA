// Same-origin proxy: forwards /api/* from the browser to the backend API
// (server-side), so the browser never needs CORS or the API's real URL, and
// the API can remain internal-only. SSE responses are streamed through.
import { NextRequest } from "next/server";
import { parseEnabledFlag } from "@/lib/envFlags";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8080";
const DEV_USER = process.env.DEV_USER;
const CHAT_PILOT_UI_ENABLED = parseEnabledFlag(process.env.CHAT_PILOT_UI_ENABLED);
const CHAT_PILOT_API_ALLOWED_HOST = process.env.CHAT_PILOT_API_ALLOWED_HOST?.trim().toLowerCase();
const PILOT_SESSION_ID = /^[A-Za-z0-9_-]+$/;

const HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "host",
  "content-length",
]);

function encodeApiPath(path: string[]): string | null {
  if (path.length === 0) return null;
  const encoded: string[] = [];
  for (const segment of path) {
    if (
      !segment ||
      segment === "." ||
      segment === ".." ||
      /[/\\?#\u0000-\u001f\u007f]/.test(segment)
    ) {
      return null;
    }
    encoded.push(encodeURIComponent(segment));
  }
  return encoded.join("/");
}

function pilotRouteAllowed(method: string, path: string[]): boolean {
  if (method === "GET" && path.length === 1 && path[0] === "models") return true;
  if (path.length === 1 && path[0] === "sessions" && ["GET", "POST"].includes(method)) return true;
  if (
    method === "GET" &&
    path[0] === "sessions" &&
    path.length === 2 &&
    !["initializations", "deletions"].includes(path[1]) &&
    PILOT_SESSION_ID.test(path[1])
  ) return true;
  if (
    method === "GET" &&
    path[0] === "sessions" &&
    path.length === 3 &&
    !["initializations", "deletions"].includes(path[1]) &&
    PILOT_SESSION_ID.test(path[1]) &&
    path[2] === "messages"
  ) return true;
  return method === "POST" && path.length === 1 && path[0] === "chat";
}

function pilotApiBaseUrl(): string | null {
  const allowedHost = CHAT_PILOT_API_ALLOWED_HOST;
  if (
    !allowedHost ||
    !allowedHost.includes(".internal.") ||
    !allowedHost.endsWith(".azurecontainerapps.io") ||
    allowedHost.endsWith(".") ||
    /[/:?#@*]/.test(allowedHost)
  ) {
    return null;
  }
  try {
    const apiUrl = new URL(API_BASE_URL);
    if (
      apiUrl.protocol !== "https:" ||
      apiUrl.username ||
      apiUrl.password ||
      apiUrl.port ||
      apiUrl.pathname !== "/" ||
      apiUrl.search ||
      apiUrl.hash ||
      apiUrl.hostname !== allowedHost
    ) {
      return null;
    }
    return apiUrl.origin;
  } catch {
    return null;
  }
}

async function forward(req: NextRequest, path: string[]): Promise<Response> {
  const encodedPath = encodeApiPath(path);
  if (!encodedPath) {
    return Response.json({ detail: "Invalid API path" }, { status: 400 });
  }
  const method = req.method.toUpperCase();
  if (CHAT_PILOT_UI_ENABLED && !pilotRouteAllowed(method, path)) {
    return Response.json(
      {
        detail: "This route is unavailable in isolated chat-pilot mode.",
        code: "chat_pilot_route_disabled",
      },
      { status: 404, headers: { "Cache-Control": "no-store" } },
    );
  }
  const apiBaseUrl = CHAT_PILOT_UI_ENABLED ? pilotApiBaseUrl() : API_BASE_URL;
  if (!apiBaseUrl) {
    return Response.json(
      { detail: "Pilot API target is not configured.", code: "chat_pilot_api_target_unavailable" },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
  const search = req.nextUrl.search;
  const target = `${apiBaseUrl.replace(/\/$/, "")}/api/${encodedPath}${search}`;

  // Headers named by an inbound `Connection` header are also hop-by-hop.
  const reqDynamicHop = (req.headers.get("connection") ?? "")
    .split(",")
    .map((h) => h.trim().toLowerCase())
    .filter(Boolean);

  const headers = new Headers();
  req.headers.forEach((value, key) => {
    const lk = key.toLowerCase();
    if (HOP_BY_HOP.has(lk) || reqDynamicHop.includes(lk)) return;
    headers.set(key, value);
  });
  // Dev-auth identity: the proxy is the sole authority for X-Dev-User so the
  // browser can't spoof another user. Overwrite when configured, else drop it.
  if (DEV_USER) headers.set("x-dev-user", DEV_USER);
  else headers.delete("x-dev-user");

  const hasBody = method !== "GET" && method !== "HEAD";
  const init: RequestInit & { duplex?: "half" } = {
    method,
    headers,
    redirect: "manual",
  };
  if (
    (method === "POST" && encodedPath === "decisions/run") ||
    (method === "GET" && encodedPath === "decisions/options")
  ) {
    init.signal = req.signal;
  }
  if (hasBody) {
    init.body = req.body;
    init.duplex = "half";
  }

  let upstream: Response;
  try {
    upstream = await fetch(target, init);
    if (CHAT_PILOT_UI_ENABLED && upstream.status >= 300 && upstream.status < 400) {
      await upstream.body?.cancel();
      return Response.json(
        {
          detail: "API upstream redirect refused in isolated chat-pilot mode.",
          code: "chat_pilot_upstream_redirect",
        },
        { status: 502, headers: { "Cache-Control": "no-store" } },
      );
    }
  } catch {
    return Response.json(
      { detail: "API upstream unavailable" },
      {
        status: 502,
        ...(CHAT_PILOT_UI_ENABLED ? { headers: { "Cache-Control": "no-store" } } : {}),
      },
    );
  }

  const respDynamicHop = (upstream.headers.get("connection") ?? "")
    .split(",")
    .map((h) => h.trim().toLowerCase())
    .filter(Boolean);

  const respHeaders = new Headers();
  upstream.headers.forEach((value, key) => {
    const lk = key.toLowerCase();
    if (HOP_BY_HOP.has(lk) || respDynamicHop.includes(lk)) return;
    respHeaders.set(key, value);
  });

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: respHeaders,
  });
}

type Ctx = { params: Promise<{ path: string[] }> };

export async function GET(req: NextRequest, { params }: Ctx) {
  return forward(req, (await params).path);
}
export async function POST(req: NextRequest, { params }: Ctx) {
  return forward(req, (await params).path);
}
export async function PATCH(req: NextRequest, { params }: Ctx) {
  return forward(req, (await params).path);
}
export async function PUT(req: NextRequest, { params }: Ctx) {
  return forward(req, (await params).path);
}
export async function DELETE(req: NextRequest, { params }: Ctx) {
  return forward(req, (await params).path);
}
