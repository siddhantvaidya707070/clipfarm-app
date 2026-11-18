import { GET, POST } from "./route";
import { auth } from "@/lib/auth";
vi.mock("next/headers", () => ({ headers: vi.fn().mockResolvedValue(new Headers()) }));
vi.mock("@/lib/auth", () => ({ auth: { api: { getSession: vi.fn() } } }));
vi.mock("@/lib/backend-auth", () => ({ buildBackendAuthHeaders: () => ({ "x-supoclip-user-id": "owner" }) }));
describe("native capture proxy", () => {
  beforeEach(() => { vi.stubGlobal("fetch", vi.fn()); vi.stubEnv("BETTER_AUTH_URL", "http://localhost:3107"); vi.stubEnv("NEXT_PUBLIC_APP_URL", "http://localhost:3107"); vi.mocked(auth.api.getSession).mockResolvedValue({ user: { id: "owner" } } as never); });
  afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });
  it("requires a native login", async () => {
    vi.mocked(auth.api.getSession).mockResolvedValue(null as never);
    expect((await GET(new Request("http://localhost:3107/api/capture"))).status).toBe(401);
    expect(fetch).not.toHaveBeenCalled();
  });
  it("rejects cross-origin state changes", async () => {
    expect((await POST(new Request("http://localhost:3107/api/capture", { method: "POST", headers: { Origin: "https://example.com" }, body: "{}" }))).status).toBe(403);
    expect(fetch).not.toHaveBeenCalled();
  });
  it("signs owner requests without a browser token", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response("[]", { status: 200 }));
    expect((await GET(new Request("http://localhost:3107/api/capture"))).status).toBe(200);
    expect(fetch).toHaveBeenCalledWith("http://supervisor:8787/native/capture", expect.objectContaining({ headers: expect.objectContaining({ "x-supoclip-user-id": "owner" }) }));
  });
  it("accepts the configured browser origin behind the standalone bind address", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response("[]", { status: 200 }));
    const response = await POST(new Request("http://0.0.0.0:3107/api/capture", { method: "POST", headers: { origin: "http://localhost:3107" }, body: "{}" }));
    expect(response.status).toBe(200);
    expect(fetch).toHaveBeenCalled();
  });
  it("rejects untrusted origins behind the standalone bind address", async () => {
    const response = await POST(new Request("http://0.0.0.0:3107/api/capture", { method: "POST", headers: { origin: "https://example.com" }, body: "{}" }));
    expect(response.status).toBe(403);
    expect(fetch).not.toHaveBeenCalled();
  });
});
