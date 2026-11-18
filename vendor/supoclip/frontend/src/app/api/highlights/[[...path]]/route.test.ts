import { DELETE, GET, PATCH, POST } from "./route";
import { auth } from "@/lib/auth";

vi.mock("next/headers", () => ({ headers: vi.fn().mockResolvedValue(new Headers()) }));
vi.mock("@/lib/auth", () => ({ auth: { api: { getSession: vi.fn() } } }));
vi.mock("@/lib/backend-auth", () => ({ buildBackendAuthHeaders: () => ({ "x-supoclip-user-id": "owner", "x-supoclip-signature": "signed" }) }));
const context = (path: string[] = []) => ({ params: Promise.resolve({ path }) });
const request = (method = "GET", suffix = "", body?: string) => new Request(`http://localhost:3107/api/highlights${suffix}`, { method, headers: { origin: "http://localhost:3107" }, body });

describe("native highlight proxy", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ streams: [] })));
    vi.mocked(auth.api.getSession).mockResolvedValue({ user: { id: "owner" } } as never);
    vi.stubEnv("CAPTURE_INTERNAL_URL", "http://supervisor:8787");
    vi.stubEnv("BETTER_AUTH_URL", "http://localhost:3107");
    vi.stubEnv("NEXT_PUBLIC_APP_URL", "http://localhost:3107");
  });
  afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });
  it("requires a server session for file downloads", async () => {
    vi.mocked(auth.api.getSession).mockResolvedValue(null as never);
    expect((await GET(request(), context(["stream", "files", "clip.mp4"]))).status).toBe(401);
    expect(fetch).not.toHaveBeenCalled();
  });
  it.each([POST, PATCH, DELETE])("rejects cross-origin mutations", async handler => {
    const response = await handler(new Request("http://localhost:3107/api/highlights", { method: "POST", headers: { origin: "https://example.com" }, body: "{}" }), context());
    expect(response.status).toBe(403);
    expect(fetch).not.toHaveBeenCalled();
  });
  it("accepts the configured browser origin behind the standalone bind address", async () => {
    const response = await POST(new Request("http://0.0.0.0:3107/api/highlights", { method: "POST", headers: { origin: "http://localhost:3107" }, body: "{}" }), context());
    expect(response.status).toBe(200);
    expect(fetch).toHaveBeenCalled();
  });
  it("rejects other origins even when the internal bind origin differs", async () => {
    const response = await POST(new Request("http://0.0.0.0:3107/api/highlights", { method: "POST", headers: { origin: "https://example.com" }, body: "{}" }), context());
    expect(response.status).toBe(403);
    expect(fetch).not.toHaveBeenCalled();
  });
  it("uses the configured public app origin when the auth URL is unset", async () => {
    vi.stubEnv("BETTER_AUTH_URL", "");
    const response = await POST(new Request("http://0.0.0.0:3107/api/highlights", { method: "POST", headers: { origin: "http://localhost:3107" }, body: "{}" }), context());
    expect(response.status).toBe(200);
  });
  it("signs owner requests without exposing the supervisor address", async () => {
    expect((await GET(request(), context())).status).toBe(200);
    const [url, init] = vi.mocked(fetch).mock.calls[0];
    expect(url).toBe("http://supervisor:8787/native/highlights");
    expect(new Headers(init?.headers).get("x-supoclip-signature")).toBe("signed");
  });
  it("streams partial video responses with range metadata", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(new Uint8Array([1, 2, 3]), { status: 206, headers: { "Content-Type": "video/mp4", "Content-Range": "bytes 0-2/10", "Accept-Ranges": "bytes", "Content-Length": "3" } }));
    const response = await GET(new Request("http://localhost:3107/api/highlights/stream/files/clip.mp4", { headers: { Range: "bytes=0-2" } }), context(["stream", "files", "clip.mp4"]));
    expect(response.status).toBe(206);
    expect(response.headers.get("content-type")).toBe("video/mp4");
    expect(response.headers.get("content-range")).toBe("bytes 0-2/10");
    expect(new Headers(vi.mocked(fetch).mock.calls[0][1]?.headers).get("range")).toBe("bytes=0-2");
    expect(new Uint8Array(await response.arrayBuffer())).toEqual(new Uint8Array([1, 2, 3]));
  });
  it("preserves caption and manifest download headers", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response("{}", { headers: { "Content-Type": "application/json", "Content-Disposition": 'attachment; filename="manifest.json"' } }));
    const response = await GET(request(), context(["stream", "manifest"]));
    expect(response.headers.get("content-disposition")).toBe('attachment; filename="manifest.json"');
  });
  it("rejects unknown routes and malformed JSON", async () => {
    expect((await GET(request(), context(["..", "capture"]))).status).toBe(404);
    expect((await POST(request("POST", "", "{"), context())).status).toBe(400);
    expect((await PATCH(request("PATCH", "", "[]"), context(["stream", "clips", "clip"]))).status).toBe(400);
    expect(fetch).not.toHaveBeenCalled();
  });
  it("routes edits and deletion without accepting arbitrary upstream URLs", async () => {
    const response = await PATCH(request("PATCH", "/stream/clips/clip", '{"start":1,"end":4}'), context(["stream", "clips", "clip"]));
    expect(response.status).toBe(200);
    expect(fetch).toHaveBeenCalledWith("http://supervisor:8787/native/highlights/stream/clips/clip", expect.objectContaining({ method: "PATCH", body: '{"start":1,"end":4}' }));
  });
  it("returns a helpful error when the service is unavailable", async () => {
    vi.mocked(fetch).mockRejectedValue(new Error("internal private address"));
    const response = await GET(request(), context());
    expect(response.status).toBe(503);
    expect(await response.text()).not.toContain("internal private address");
  });
});
