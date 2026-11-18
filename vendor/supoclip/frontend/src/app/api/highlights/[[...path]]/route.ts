import { headers } from "next/headers";
import { NextResponse } from "next/server";
import { auth } from "@/lib/auth";
import { buildBackendAuthHeaders } from "@/lib/backend-auth";

type Context = { params: Promise<{ path?: string[] }> };
const ID = "[a-zA-Z0-9_-]{1,80}";
const allowed: Record<string, RegExp> = {
  GET: new RegExp(`^(?:|/${ID}|/${ID}/manifest|/${ID}/files/${ID}\\.(?:mp4|srt))$`),
  POST: new RegExp(`^(?:|/${ID}/(?:retry|stop|clips|merge|export))$`),
  PATCH: new RegExp(`^/${ID}/clips/${ID}$`),
  DELETE: new RegExp(`^/${ID}/clips/${ID}$`),
};

async function proxy(request: Request, context: Context) {
  const session = await auth.api.getSession({ headers: await headers() });
  if (!session?.user?.id) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  const expectedOrigin = new URL(process.env.BETTER_AUTH_URL || process.env.NEXT_PUBLIC_APP_URL || request.url).origin;
  if (request.method !== "GET" && request.headers.get("origin") !== expectedOrigin) {
    return NextResponse.json({ error: "Invalid origin" }, { status: 403 });
  }
  const { path = [] } = await context.params;
  const suffix = path.length ? `/${path.join("/")}` : "";
  if (!allowed[request.method]?.test(suffix)) return NextResponse.json({ error: "Unknown highlight endpoint" }, { status: 404 });
  let body: string | undefined;
  if (request.method === "POST" || request.method === "PATCH") {
    const raw = await request.text();
    try {
      const parsed = raw ? JSON.parse(raw) : {};
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
      body = JSON.stringify(parsed);
    } catch { return NextResponse.json({ error: "Invalid JSON request" }, { status: 400 }); }
  }
  const upstreamHeaders = new Headers(buildBackendAuthHeaders(session.user.id));
  if (body !== undefined) upstreamHeaders.set("Content-Type", "application/json");
  for (const name of ["range", "if-range"]) {
    const value = request.headers.get(name);
    if (request.method === "GET" && value) upstreamHeaders.set(name, value);
  }
  try {
    const result = await fetch(`${process.env.CAPTURE_INTERNAL_URL || "http://supervisor:8787"}/native/highlights${suffix}`, {
      method: request.method, headers: upstreamHeaders, body, cache: "no-store", signal: request.signal,
    });
    const responseHeaders = new Headers({ "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" });
    for (const name of ["content-type", "content-length", "content-disposition", "content-range", "accept-ranges", "etag", "last-modified"]) {
      const value = result.headers.get(name);
      if (value) responseHeaders.set(name, value);
    }
    return new Response(result.body, { status: result.status, headers: responseHeaders });
  } catch {
    return NextResponse.json({ error: "Highlight service unavailable. Try again shortly." }, { status: 503 });
  }
}

export const GET = proxy;
export const POST = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
