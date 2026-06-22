import { beforeEach, describe, expect, it, vi } from "vitest";

const { fetchMock } = vi.hoisted(() => ({ fetchMock: vi.fn() }));

vi.stubGlobal("fetch", fetchMock);
vi.mock("../src/utils/io.js", () => ({
  readTextFileSync: () =>
    JSON.stringify({ server: { url: "http://127.0.0.1:1810", token: "test-token" } }),
  writeTextFileSync: vi.fn(),
}));
vi.mock("../src/utils/logger.js", () => ({
  logger: { info: vi.fn(), warn: vi.fn(), error: vi.fn(), debug: vi.fn() },
}));

import { _resetCatalogCache, evictCatalogSession, getCatalog } from "../src/services/catalog.js";

function mockCatalogResponse(text: string) {
  fetchMock.mockResolvedValue({
    ok: true,
    json: () => Promise.resolve({ code: 0, data: { catalog: text } }),
  });
}

describe("getCatalog", () => {
  beforeEach(() => {
    _resetCatalogCache();
    vi.clearAllMocks();
  });

  it("fetches catalog from backend with session_key", async () => {
    mockCatalogResponse("# catalog\n");
    const out = await getCatalog("session-1");
    expect(out).toBe("# catalog\n");
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:1810/api/miot/catalog?session_key=session-1",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("returns empty string on HTTP error", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 500 });
    expect(await getCatalog("session-1")).toBe("");
  });

  it("returns empty string on network error", async () => {
    fetchMock.mockRejectedValue(new Error("ECONNREFUSED"));
    expect(await getCatalog("session-1")).toBe("");
  });

  it("throttles within 5s when no sessionKey, returns cached result", async () => {
    mockCatalogResponse("# v1\n");
    await getCatalog(); // no sessionKey
    const out = await getCatalog(); // throttled — should return cached
    expect(out).toBe("# v1\n");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("evictCatalogSession", () => {
  it("sends DELETE request", async () => {
    fetchMock.mockResolvedValue({ ok: true });
    await evictCatalogSession("session-1");
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:1810/api/miot/catalog/session-1",
      expect.objectContaining({ method: "DELETE" }),
    );
  });
});
