import { afterEach, describe, expect, it, vi } from "vitest";

import {
  productApiUrl,
  publicApiUrl,
  publicSnapshotUrl,
  requestProductJson,
  requestPublicJson,
} from "@/lib/api";
import type { RuntimeConfig } from "@/types";

const config: RuntimeConfig = {
  publicApiBase: "/v1",
  productApiBase: "/v2",
  snapshotBase: "https://snapshots.example.test/current.json",
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("API trust surfaces", () => {
  it("keeps public requests credential-free and strips Authorization", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () =>
      new Response(JSON.stringify({ mode: "public" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await requestPublicJson(config, "/incidents", {
      headers: { Authorization: "Bearer must-not-leak" },
    });

    const [url, requestInit] = fetchMock.mock.calls[0];
    const init = requestInit as RequestInit;
    expect(url).toBe("/v1/incidents");
    expect(init.credentials).toBe("omit");
    expect(new Headers(init.headers).has("authorization")).toBe(false);
  });

  it("requires a Bearer token and sends it only to the product base", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () =>
      new Response(JSON.stringify({ effective_role: "viewer" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await requestProductJson(config, "/me", "access-token", {
      headers: { Authorization: "Bearer caller-cannot-override" },
    });

    const [url, requestInit] = fetchMock.mock.calls[0];
    const init = requestInit as RequestInit;
    expect(url).toBe("/v2/me");
    expect(init.credentials).toBe("omit");
    expect(new Headers(init.headers).get("authorization")).toBe("Bearer access-token");
  });

  it("fails before network access when a product token is absent", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await expect(requestProductJson(config, "/me", "")).rejects.toMatchObject({ status: 401 });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("keeps URL selection explicit for public, product, and static snapshots", () => {
    expect(publicApiUrl(config, "/runs/1")).toBe("/v1/runs/1");
    expect(productApiUrl(config, "/runs/1")).toBe("/v2/runs/1");
    expect(publicSnapshotUrl(config, "tenant:demo", "2026-08-09T00:00:00Z")).toBe(
      "https://snapshots.example.test/current.json?namespace=tenant%3Ademo&as_of=2026-08-09T00%3A00%3A00Z",
    );
  });
});
