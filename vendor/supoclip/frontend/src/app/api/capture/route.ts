import { headers } from "next/headers";
import { NextResponse } from "next/server";
import { auth } from "@/lib/auth";
import { buildBackendAuthHeaders } from "@/lib/backend-auth";

export async function GET(request: Request) { return proxy(request); }
export async function POST(request: Request) { return proxy(request); }

async function proxy(request: Request) {
  const session = await auth.api.getSession({ headers: await headers() });
  if (!session?.user?.id) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  const expectedOrigin = new URL(process.env.BETTER_AUTH_URL || process.env.NEXT_PUBLIC_APP_URL || request.url).origin;
  if (request.method === "POST" && request.headers.get("origin") !== expectedOrigin) {
    return NextResponse.json({ error: "Invalid origin" }, { status: 403 });
  }
  let body;
  if (request.method === "POST") {
    try { body = await request.json(); }
    catch { return NextResponse.json({ error: "Invalid request" }, { status: 400 }); }
  }
  let path = "/native/capture";
  if (body?.action === "stop") {
    if (!/^[a-f0-9]{32}$/.test(body.id ?? "")) return NextResponse.json({ error: "Invalid capture" }, { status: 400 });
    path += `/${body.id}/stop`;
  }
  try {
    const result = await fetch(`${process.env.CAPTURE_INTERNAL_URL || "http://supervisor:8787"}${path}`, {
      method: request.method,
      headers: { ...buildBackendAuthHeaders(session.user.id), "Content-Type": "application/json" },
      ...(body ? { body: JSON.stringify(body) } : {}),
      cache: "no-store", signal: AbortSignal.timeout(15000),
    });
    return new NextResponse(await result.text(), { status: result.status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
  } catch {
    return NextResponse.json({ error: "Capture service unavailable" }, { status: 503 });
  }
}
