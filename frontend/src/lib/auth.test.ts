import { webcrypto } from "node:crypto";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  HostedUiAuthAdapter,
  PKCE_TRANSACTION_KEY,
  PKCE_TRANSACTION_TTL_MS,
} from "@/lib/auth";
import type { HostedUiAuthConfig } from "@/types";

const config: HostedUiAuthConfig = {
  hostedUiBaseUrl: "https://auth.example.test",
  clientId: "public-browser-client",
  redirectUri: "https://app.example.test/callback",
  logoutUri: "https://app.example.test/",
  scopes: ["openid", "hindsight/read"],
};

const fixedNow = 1_800_000_000_000;

function testRuntime(fetchMock = vi.fn<typeof fetch>()) {
  let current = new URL("https://app.example.test/replay?namespace=payments");
  const navigations: string[] = [];
  const replacements: string[] = [];
  return {
    runtime: {
      crypto: webcrypto as Crypto,
      storage: window.sessionStorage,
      fetch: fetchMock,
      now: () => fixedNow,
      currentUrl: () => new URL(current),
      navigate: (url: string) => navigations.push(url),
      replaceUrl: (url: string) => replacements.push(url),
    },
    navigations,
    replacements,
    setCurrent: (url: string) => {
      current = new URL(url);
    },
  };
}

function storedTransaction() {
  return JSON.parse(String(sessionStorage.getItem(PKCE_TRANSACTION_KEY))) as {
    state: string;
    verifier: string;
    created_at: number;
    return_to: string;
  };
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
});

afterEach(() => vi.restoreAllMocks());

describe("Hosted UI authorization-code PKCE", () => {
  it("creates an S256 authorization request and stores only the one-use transaction", async () => {
    const harness = testRuntime();
    const adapter = new HostedUiAuthAdapter(config, harness.runtime);

    await adapter.signIn("/replay?namespace=payments");

    const transaction = storedTransaction();
    const authorization = new URL(harness.navigations[0]);
    const digest = await webcrypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(transaction.verifier),
    );
    expect(authorization.pathname).toBe("/oauth2/authorize");
    expect(authorization.searchParams.get("response_type")).toBe("code");
    expect(authorization.searchParams.get("client_id")).toBe(config.clientId);
    expect(authorization.searchParams.get("code_challenge_method")).toBe("S256");
    expect(authorization.searchParams.get("code_challenge")).toBe(
      Buffer.from(digest).toString("base64url"),
    );
    expect(authorization.searchParams.has("client_secret")).toBe(false);
    expect(transaction).toMatchObject({
      state: authorization.searchParams.get("state"),
      created_at: fixedNow,
      return_to: "/replay?namespace=payments",
    });
    expect(JSON.stringify(transaction)).not.toContain("access_token");
  });

  it("exchanges a matching callback without persisting any returned token", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () =>
      new Response(
        JSON.stringify({
          access_token: "memory-only-access",
          expires_in: 900,
          token_type: "Bearer",
          id_token: "discarded-id-token",
          refresh_token: "discarded-refresh-token",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    const harness = testRuntime(fetchMock);
    const adapter = new HostedUiAuthAdapter(config, harness.runtime);
    await adapter.signIn("/replay?namespace=payments");
    const transaction = storedTransaction();
    harness.setCurrent(
      `https://app.example.test/callback?code=authorization-code&state=${transaction.state}`,
    );

    const session = await adapter.initialize();

    expect(session).toEqual({
      accessToken: "memory-only-access",
      expiresAt: fixedNow + 900_000,
    });
    expect(adapter.accessToken()).toBe("memory-only-access");
    const [input, init] = fetchMock.mock.calls[0];
    expect(String(input)).toBe("https://auth.example.test/oauth2/token");
    expect(init?.credentials).toBe("omit");
    const body = init?.body as URLSearchParams;
    expect(body.get("grant_type")).toBe("authorization_code");
    expect(body.get("code_verifier")).toBe(transaction.verifier);
    expect(body.has("client_secret")).toBe(false);
    expect(sessionStorage.length).toBe(0);
    expect(localStorage.length).toBe(0);
    expect(harness.replacements).toEqual(["/replay?namespace=payments"]);
  });

  it.each([
    ["mismatched", fixedNow],
    ["expired", fixedNow - PKCE_TRANSACTION_TTL_MS - 1],
  ])("rejects a %s callback before token exchange", async (mode, createdAt) => {
    const fetchMock = vi.fn<typeof fetch>();
    const harness = testRuntime(fetchMock);
    const adapter = new HostedUiAuthAdapter(config, harness.runtime);
    await adapter.signIn();
    const transaction = storedTransaction();
    transaction.created_at = createdAt;
    sessionStorage.setItem(PKCE_TRANSACTION_KEY, JSON.stringify(transaction));
    const state = mode === "mismatched" ? "attacker-state" : transaction.state;
    harness.setCurrent(`https://app.example.test/callback?code=code&state=${state}`);

    await expect(adapter.initialize()).rejects.toThrow(/invalid or expired/i);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(sessionStorage.length).toBe(0);
  });

  it("clears the in-memory session before Hosted UI logout", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () =>
      new Response(JSON.stringify({ access_token: "temporary", expires_in: 900, token_type: "Bearer" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const harness = testRuntime(fetchMock);
    const adapter = new HostedUiAuthAdapter(config, harness.runtime);
    await adapter.signIn();
    const transaction = storedTransaction();
    harness.setCurrent(`https://app.example.test/callback?code=code&state=${transaction.state}`);
    await adapter.initialize();

    adapter.signOut();

    expect(adapter.accessToken()).toBeNull();
    const logout = new URL(harness.navigations.at(-1) as string);
    expect(logout.pathname).toBe("/logout");
    expect(logout.searchParams.get("client_id")).toBe(config.clientId);
    expect(logout.searchParams.get("logout_uri")).toBe(config.logoutUri);
  });
});
