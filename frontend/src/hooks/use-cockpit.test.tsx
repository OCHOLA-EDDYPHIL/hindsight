import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useCockpit } from "@/hooks/use-cockpit";
import { LocalAuthAdapter } from "@/test/local-auth-adapter";
import type { EffectiveIdentity, SignatureScenario, Snapshot } from "@/types";

const currentSnapshot: Snapshot = {
  mode: "current",
  namespace: "test:history-race",
  as_of: null,
  timeline: ["2026-07-17T10:00:00Z", "2026-07-17T11:00:00Z"],
  memories: [],
  operations: [],
};

const historicalSnapshot: Snapshot = {
  ...currentSnapshot,
  mode: "as_of",
  as_of: currentSnapshot.timeline[0],
};

function approvalScenario(withIdentity = true): SignatureScenario {
  return {
    scenario_id: "scenario-approval",
    namespace: currentSnapshot.namespace,
    status: "active",
    session_status: "active",
    rewind_anchor: null,
    completed_at: null,
    incident: {
      slug: "incident-approval",
      title: "Checkout latency",
      summary: "Processor timeouts increased checkout latency.",
    },
    runs: [
      {
        id: "run-approval",
        status: "awaiting_approval",
        action_trace: withIdentity
          ? {
              mode: "recommendation_only",
              selection: { fingerprint: "b".repeat(64) },
              recommendation: {
                id: `recommendation:${"a".repeat(64)}`,
                summary: "Throttle retry fanout after verifying processor health.",
              },
            }
          : { mode: "recommendation_only" },
      },
    ],
    memories: [],
    stages: {},
  };
}

function remediationApprovalScenario(): SignatureScenario {
  const scenario = approvalScenario();
  return {
    ...scenario,
    runs: [
      {
        id: "run-approval",
        status: "awaiting_approval",
        action_trace: {
          schema_version: 3,
          mode: "governed_memory_remediation",
          selection: { fingerprint: "b".repeat(64) },
          observation_fingerprint: "c".repeat(64),
          remediation_action: {
            id: `remediation_action:${"a".repeat(64)}`,
            name: "retract_recalled_memory",
            target_memory_id: "memory-unsafe",
            target_excerpt: "Increase retry fanout during saturation.",
          },
          preview: {
            id: "preview-1",
            fingerprint: "d".repeat(64),
            effect_count: 2,
            effects: {
              close_memory_ids: ["memory-unsafe"],
              review_resolutions: [
                {
                  id: "review-unsafe",
                  semantic_memory_id: "memory-unsafe",
                  status: "superseded",
                },
              ],
            },
          },
        },
      },
    ],
  };
}

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function effectiveIdentity(role: "viewer" | "operator" = "operator"): EffectiveIdentity {
  return {
    principal_id: "principal-1",
    tenant_id: "tenant-1",
    tenant_slug: "payments",
    token_role: role,
    mapped_role: role,
    effective_role: role,
    scopes: role === "operator" ? ["read", "realtime", "write"] : ["read", "realtime"],
    expires_at: 4_102_444_800,
  };
}

function signedInAdapter() {
  return new LocalAuthAdapter({
    accessToken: "test-access-token",
    expiresAt: Date.now() + 60_000,
  });
}

const fakeSocketOpen = 1;
let fakeSocketInstances: FakeWebSocket[] = [];

class FakeWebSocket {

  readyState = 0;
  listeners = new Map<string, Array<(event: any) => void>>();
  sent: string[] = [];

  constructor(public url: string) {
    fakeSocketInstances.push(this);
  }

  addEventListener(type: string, listener: (event: any) => void) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  send(payload: string) {
    this.sent.push(payload);
  }

  open() {
    this.readyState = fakeSocketOpen;
    this.emit("open", {});
  }

  message(payload: unknown) {
    this.emit("message", { data: JSON.stringify(payload) });
  }

  serverClose() {
    this.readyState = 3;
    this.emit("close", {});
  }

  close() {
    if (this.readyState === 3) return;
    this.serverClose();
  }

  private emit(type: string, event: unknown) {
    for (const listener of this.listeners.get(type) || []) listener(event);
  }
}

Object.assign(FakeWebSocket, { OPEN: fakeSocketOpen });

function realtimeReference(eventId: string, hlc: string, type = "memory") {
  return {
    version: 2,
    event_id: eventId,
    cursor: { hlc, event_id: eventId },
    type,
    namespace: currentSnapshot.namespace,
    run_id: null,
    data: { reference: { id: `${type}-${eventId}` } },
  };
}

describe("cockpit historical snapshot selection", () => {
  beforeEach(() => {
    fakeSocketInstances = [];
    window.history.replaceState({}, "", "/");
    window.HINDSIGHT_CONFIG = {
      publicApiBase: "/v1",
      productApiBase: "/v2",
      snapshotBase: "/snapshot",
      defaultNamespace: currentSnapshot.namespace,
      pollIntervalMs: 1500,
    };
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    window.HINDSIGHT_CONFIG = {};
  });

  it("does not let live polling supersede a pending historical request", async () => {
    let poll: (() => void) | undefined;
    const setInterval = window.setInterval.bind(window);
    vi.spyOn(window, "setInterval").mockImplementation(
      ((handler: TimerHandler, timeout?: number, ...args: any[]) => {
        if (timeout === 1500) {
          poll = handler as () => void;
          return 1;
        }
        return setInterval(handler, timeout, ...args);
      }) as typeof window.setInterval,
    );

    let resolveHistorical: (response: Response) => void = () => undefined;
    const pendingHistorical = new Promise<Response>((resolve) => {
      resolveHistorical = resolve;
    });
    let liveSnapshotRequests = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = new URL(String(input), window.location.origin);
        if (url.pathname === "/v1/incidents") {
          return Promise.resolve(jsonResponse({ items: [] }));
        }
        if (url.pathname === "/snapshot" && url.searchParams.has("as_of")) {
          return pendingHistorical;
        }
        if (url.pathname === "/snapshot") {
          liveSnapshotRequests += 1;
          return Promise.resolve(jsonResponse(currentSnapshot));
        }
        return Promise.reject(new Error(`unexpected request: ${url}`));
      }),
    );

    const { result } = renderHook(() => useCockpit());
    await waitFor(() => expect(result.current.loadState).toBe("ready"));
    expect(liveSnapshotRequests).toBe(1);
    expect(poll).toBeTypeOf("function");

    let selection: Promise<void> | undefined;
    act(() => {
      selection = result.current.selectHistorical(currentSnapshot.timeline[0]);
    });
    await act(async () => {
      poll?.();
      await Promise.resolve();
    });

    expect(liveSnapshotRequests).toBe(1);
    resolveHistorical(jsonResponse(historicalSnapshot));
    await act(async () => selection);

    expect(result.current.snapshot?.as_of).toBe(currentSnapshot.timeline[0]);
    expect(result.current.connection).toBe("historical");
  });

  it("refetches a live snapshot for sanitized realtime references", async () => {
    window.HINDSIGHT_CONFIG = {
      ...window.HINDSIGHT_CONFIG,
      websocketUrl: "wss://socket.example.test/demo",
    };
    let snapshotRequests = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = new URL(String(input), window.location.origin);
        if (url.pathname === "/v1/incidents") {
          return Promise.resolve(jsonResponse({ items: [] }));
        }
        if (url.pathname === "/v1/realtime/ticket") {
          return Promise.resolve(jsonResponse({ ticket: "signed-ticket" }));
        }
        if (url.pathname === "/snapshot") {
          snapshotRequests += 1;
          return Promise.resolve(jsonResponse(currentSnapshot));
        }
        return Promise.reject(new Error(`unexpected request: ${url}`));
      }),
    );

    vi.stubGlobal("WebSocket", FakeWebSocket);

    const { result } = renderHook(() => useCockpit());
    await waitFor(() => expect(result.current.loadState).toBe("ready"));
    await waitFor(() => expect(fakeSocketInstances).toHaveLength(1));
    const latestSocket = fakeSocketInstances[0];
    expect(String(latestSocket?.url)).toContain("ticket=signed-ticket");
    expect(snapshotRequests).toBe(1);

    act(() => {
      latestSocket.message({
        type: "memory",
        data: { reference: { id: "memory-1", status: "active" } },
      });
    });

    await waitFor(() => expect(snapshotRequests).toBe(2));
  });

  it("deduplicates replayed v2 events and reconciles unseen events below high-water", async () => {
    window.HINDSIGHT_CONFIG = {
      ...window.HINDSIGHT_CONFIG,
      websocketUrl: "wss://socket.example.test/demo",
    };
    let snapshotRequests = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = new URL(String(input), window.location.origin);
        if (url.pathname === "/v1/incidents") {
          return Promise.resolve(jsonResponse({ items: [] }));
        }
        if (url.pathname === "/v1/realtime/ticket") {
          return Promise.resolve(jsonResponse({ ticket: "signed-ticket" }));
        }
        if (url.pathname === "/snapshot") {
          snapshotRequests += 1;
          return Promise.resolve(jsonResponse(currentSnapshot));
        }
        return Promise.reject(new Error(`unexpected request: ${url}`));
      }),
    );
    vi.stubGlobal("WebSocket", FakeWebSocket);

    const { result } = renderHook(() => useCockpit());
    await waitFor(() => expect(result.current.loadState).toBe("ready"));
    await waitFor(() => expect(fakeSocketInstances).toHaveLength(1));
    const socket = fakeSocketInstances[0];
    const latest = realtimeReference("event-latest", "30.0", "operation");

    act(() => {
      socket.message(latest);
      socket.message(latest);
    });
    await waitFor(() => expect(snapshotRequests).toBe(2));

    act(() => socket.message(realtimeReference("event-reordered", "29.0")));
    await waitFor(() => expect(snapshotRequests).toBe(3));
  });

  it("immediately fences an older snapshot response when a newer event arrives", async () => {
    window.HINDSIGHT_CONFIG = {
      ...window.HINDSIGHT_CONFIG,
      websocketUrl: "wss://socket.example.test/demo",
    };
    const staleSnapshot: Snapshot = {
      ...currentSnapshot,
      memories: [{ id: "memory-stale", content: "stale projection" }],
    };
    const latestSnapshot: Snapshot = {
      ...currentSnapshot,
      memories: [{ id: "memory-latest", content: "latest projection" }],
    };
    let snapshotRequests = 0;
    let resolveStale: (response: Response) => void = () => undefined;
    let resolveLatest: (response: Response) => void = () => undefined;
    const pendingStale = new Promise<Response>((resolve) => {
      resolveStale = resolve;
    });
    const pendingLatest = new Promise<Response>((resolve) => {
      resolveLatest = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = new URL(String(input), window.location.origin);
        if (url.pathname === "/v1/incidents") {
          return Promise.resolve(jsonResponse({ items: [] }));
        }
        if (url.pathname === "/v1/realtime/ticket") {
          return Promise.resolve(jsonResponse({ ticket: "signed-ticket" }));
        }
        if (url.pathname === "/snapshot") {
          snapshotRequests += 1;
          if (snapshotRequests === 1) return Promise.resolve(jsonResponse(currentSnapshot));
          if (snapshotRequests === 2) return pendingStale;
          if (snapshotRequests === 3) return pendingLatest;
        }
        return Promise.reject(new Error(`unexpected request: ${url}`));
      }),
    );
    vi.stubGlobal("WebSocket", FakeWebSocket);

    const { result } = renderHook(() => useCockpit());
    await waitFor(() => expect(result.current.loadState).toBe("ready"));
    await waitFor(() => expect(fakeSocketInstances).toHaveLength(1));
    const socket = fakeSocketInstances[0];

    act(() => socket.message(realtimeReference("event-before", "40.0")));
    await waitFor(() => expect(snapshotRequests).toBe(2));
    act(() => socket.message(realtimeReference("event-after", "41.0")));
    await act(async () => {
      resolveStale(jsonResponse(staleSnapshot));
      await Promise.resolve();
    });
    expect(result.current.snapshot?.memories).toEqual([]);

    await waitFor(() => expect(snapshotRequests).toBe(3));
    await act(async () => {
      resolveLatest(jsonResponse(latestSnapshot));
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.snapshot?.memories[0]?.id).toBe("memory-latest"));
  });

  it("reconciles after reconnect while ignoring replay and superseded socket messages", async () => {
    window.HINDSIGHT_CONFIG = {
      ...window.HINDSIGHT_CONFIG,
      websocketUrl: "wss://socket.example.test/demo",
    };
    let snapshotRequests = 0;
    let ticketRequests = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = new URL(String(input), window.location.origin);
        if (url.pathname === "/v1/incidents") {
          return Promise.resolve(jsonResponse({ items: [] }));
        }
        if (url.pathname === "/v1/realtime/ticket") {
          ticketRequests += 1;
          return Promise.resolve(jsonResponse({ ticket: `signed-ticket-${ticketRequests}` }));
        }
        if (url.pathname === "/snapshot") {
          snapshotRequests += 1;
          return Promise.resolve(jsonResponse(currentSnapshot));
        }
        return Promise.reject(new Error(`unexpected request: ${url}`));
      }),
    );
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const nativeSetTimeout = window.setTimeout.bind(window);
    let reconnect: (() => void) | undefined;
    vi.spyOn(window, "setTimeout").mockImplementation(
      ((handler: TimerHandler, timeout?: number, ...args: any[]) => {
        if (timeout === 1600) {
          reconnect = () => {
            if (typeof handler === "function") handler(...args);
          };
          return 99;
        }
        return nativeSetTimeout(handler, timeout, ...args);
      }) as typeof window.setTimeout,
    );

    const { result } = renderHook(() => useCockpit());
    await waitFor(() => expect(result.current.loadState).toBe("ready"));
    await waitFor(() => expect(fakeSocketInstances).toHaveLength(1));
    const first = fakeSocketInstances[0];
    act(() => first.open());
    await waitFor(() => expect(snapshotRequests).toBe(2));
    expect(JSON.parse(first.sent[0])).toMatchObject({
      type: "subscribe",
      namespace: currentSnapshot.namespace,
    });

    const delivered = realtimeReference("event-delivered", "50.0");
    act(() => first.message(delivered));
    await waitFor(() => expect(snapshotRequests).toBe(3));
    act(() => first.serverClose());
    expect(result.current.connection).toBe("reconnecting");
    expect(reconnect).toBeTypeOf("function");

    act(() => reconnect?.());
    await waitFor(() => expect(fakeSocketInstances).toHaveLength(2));
    const second = fakeSocketInstances[1];
    act(() => second.open());
    await waitFor(() => expect(snapshotRequests).toBe(4));
    const settledRequests = snapshotRequests;

    act(() => {
      second.message(delivered);
      first.message(realtimeReference("event-from-old-socket", "51.0"));
    });
    await act(async () => {
      await new Promise((resolve) => nativeSetTimeout(resolve, 150));
    });
    expect(snapshotRequests).toBe(settledRequests);
  });

  it("uses the protected one-use realtime ticket after identity resolution", async () => {
    window.HINDSIGHT_CONFIG = {
      publicApiBase: "/v1",
      productApiBase: "/v2",
      websocketUrl: "wss://socket.example.test/demo",
      defaultNamespace: currentSnapshot.namespace,
    };
    const ticketPaths: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), window.location.origin);
        if (url.pathname === "/v2/me") {
          return Promise.resolve(jsonResponse(effectiveIdentity("viewer")));
        }
        if (url.pathname === "/v2/signature-scenarios") {
          return Promise.resolve(jsonResponse(approvalScenario()));
        }
        if (url.pathname === "/v2/namespaces/test%3Ahistory-race/beliefs") {
          return Promise.resolve(jsonResponse(currentSnapshot));
        }
        if (url.pathname.endsWith("/realtime/ticket")) {
          ticketPaths.push(url.pathname);
          expect(new Headers(init?.headers).get("authorization")).toBe(
            "Bearer test-access-token",
          );
          return Promise.resolve(jsonResponse({ ticket: "protected-ticket" }));
        }
        return Promise.reject(new Error(`unexpected request: ${url}`));
      }),
    );
    vi.stubGlobal("WebSocket", FakeWebSocket);

    const authAdapter = signedInAdapter();
    const { result } = renderHook(() => useCockpit({ authAdapter }));
    await waitFor(() => expect(result.current.authStatus).toBe("authenticated"));
    await waitFor(() => expect(fakeSocketInstances).toHaveLength(1));

    expect(ticketPaths).toEqual(["/v2/realtime/ticket"]);
    expect(String(fakeSocketInstances[0].url)).toContain("ticket=protected-ticket");
    expect(String(fakeSocketInstances[0].url)).not.toContain("test-access-token");
  });

  it("fails closed when a viewer invokes a mutation callback directly", async () => {
    window.HINDSIGHT_CONFIG = {
      publicApiBase: "/v1",
      productApiBase: "/v2",
      defaultNamespace: currentSnapshot.namespace,
      pollIntervalMs: 60_000,
    };
    let mutationRequests = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), window.location.origin);
        if (init?.method === "POST") mutationRequests += 1;
        if (url.pathname === "/v2/me") {
          return Promise.resolve(jsonResponse(effectiveIdentity("viewer")));
        }
        if (url.pathname === "/v2/signature-scenarios") {
          return Promise.resolve(jsonResponse(approvalScenario()));
        }
        if (url.pathname === "/v2/namespaces/test%3Ahistory-race/beliefs") {
          return Promise.resolve(jsonResponse(currentSnapshot));
        }
        return Promise.reject(new Error(`unexpected request: ${url}`));
      }),
    );

    const authAdapter = signedInAdapter();
    const { result } = renderHook(() => useCockpit({ authAdapter }));
    await waitFor(() => expect(result.current.loadState).toBe("ready"));
    expect(result.current.canWrite).toBe(false);
    await act(async () => result.current.resetDemo());

    expect(mutationRequests).toBe(0);
    expect(result.current.notice).toEqual({
      kind: "error",
      message: "Operator authorization with write scope is required.",
    });
  });

  it("binds approval to the exact recommendation and memory selection", async () => {
    window.HINDSIGHT_CONFIG = {
      publicApiBase: "/v1",
      productApiBase: "/v2",
      defaultNamespace: currentSnapshot.namespace,
      pollIntervalMs: 60_000,
    };
    const approvalBodies: unknown[] = [];
    const requestedPaths: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), window.location.origin);
        requestedPaths.push(url.pathname);
        if (url.pathname.startsWith("/v2/")) {
          expect(new Headers(init?.headers).get("authorization")).toBe(
            "Bearer test-access-token",
          );
        }
        if (url.pathname === "/v2/me") {
          return Promise.resolve(jsonResponse(effectiveIdentity()));
        }
        if (url.pathname === "/v2/signature-scenarios") {
          return Promise.resolve(jsonResponse(approvalScenario()));
        }
        if (url.pathname === "/v2/namespaces/test%3Ahistory-race/beliefs") {
          return Promise.resolve(jsonResponse(currentSnapshot));
        }
        if (url.pathname === "/v2/runs/run-approval/approval") {
          approvalBodies.push(JSON.parse(String(init?.body)));
          return Promise.resolve(jsonResponse({ status: "resuming" }));
        }
        if (url.pathname === "/v2/runs/run-approval") {
          return Promise.resolve(
            jsonResponse({ ...approvalScenario().runs[0], status: "completed" }),
          );
        }
        return Promise.reject(new Error(`unexpected request: ${url}`));
      }),
    );

    const { result } = renderHook(() => useCockpit({ authAdapter: signedInAdapter() }));
    await waitFor(() => expect(result.current.loadState).toBe("ready"));
    expect(requestedPaths[0]).toBe("/v2/me");
    expect(result.current.canWrite).toBe(true);
    await act(async () => result.current.decideRun(true));

    expect(approvalBodies).toEqual([
      {
        approved: true,
        recommendation_id: `recommendation:${"a".repeat(64)}`,
        selection_fingerprint: "b".repeat(64),
      },
    ]);
  });

  it("binds remediation approval to action, observations, selection, and preview", async () => {
    window.HINDSIGHT_CONFIG = {
      publicApiBase: "/v1",
      productApiBase: "/v2",
      defaultNamespace: currentSnapshot.namespace,
      pollIntervalMs: 60_000,
    };
    const approvalBodies: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), window.location.origin);
        if (url.pathname === "/v2/me") {
          return Promise.resolve(jsonResponse(effectiveIdentity()));
        }
        if (url.pathname === "/v2/signature-scenarios") {
          return Promise.resolve(jsonResponse(remediationApprovalScenario()));
        }
        if (url.pathname === "/v2/namespaces/test%3Ahistory-race/beliefs") {
          return Promise.resolve(jsonResponse(currentSnapshot));
        }
        if (url.pathname === "/v2/runs/run-approval/approval") {
          approvalBodies.push(JSON.parse(String(init?.body)));
          return Promise.resolve(jsonResponse({ status: "resuming" }));
        }
        if (url.pathname === "/v2/runs/run-approval") {
          return Promise.resolve(
            jsonResponse({ ...remediationApprovalScenario().runs[0], status: "completed" }),
          );
        }
        return Promise.reject(new Error(`unexpected request: ${url}`));
      }),
    );

    const { result } = renderHook(() => useCockpit({ authAdapter: signedInAdapter() }));
    await waitFor(() => expect(result.current.loadState).toBe("ready"));
    await act(async () => result.current.decideRun(true));

    expect(approvalBodies).toEqual([
      {
        approved: true,
        remediation_action_id: `remediation_action:${"a".repeat(64)}`,
        selection_fingerprint: "b".repeat(64),
        observation_fingerprint: "c".repeat(64),
        preview_id: "preview-1",
        preview_fingerprint: "d".repeat(64),
      },
    ]);
  });

  it("fails visibly without approval-bound identities and does not post", async () => {
    window.HINDSIGHT_CONFIG = {
      publicApiBase: "/v1",
      productApiBase: "/v2",
      defaultNamespace: currentSnapshot.namespace,
      pollIntervalMs: 60_000,
    };
    let approvalRequests = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = new URL(String(input), window.location.origin);
        if (url.pathname === "/v2/me") {
          return Promise.resolve(jsonResponse(effectiveIdentity()));
        }
        if (url.pathname === "/v2/signature-scenarios") {
          return Promise.resolve(jsonResponse(approvalScenario(false)));
        }
        if (url.pathname === "/v2/namespaces/test%3Ahistory-race/beliefs") {
          return Promise.resolve(jsonResponse(currentSnapshot));
        }
        if (url.pathname.endsWith("/approval")) approvalRequests += 1;
        return Promise.reject(new Error(`unexpected request: ${url}`));
      }),
    );

    const { result } = renderHook(() => useCockpit({ authAdapter: signedInAdapter() }));
    await waitFor(() => expect(result.current.loadState).toBe("ready"));
    await act(async () => result.current.decideRun(false));

    expect(approvalRequests).toBe(0);
    expect(result.current.notice).toEqual({
      kind: "error",
      message: "Approval identity is unavailable. Refresh or rerun the analysis before deciding.",
    });
  });

  it("rehydrates an exact scenario deep link and selects the newest active run", async () => {
    const asOf = "2026-07-17T10:00:00Z";
    window.history.replaceState(
      {},
      "",
      `/?scenario_id=scenario-deep&namespace=stale&as_of=${encodeURIComponent(asOf)}`,
    );
    window.HINDSIGHT_CONFIG = {
      publicApiBase: "/v1",
      defaultNamespace: "fallback",
      pollIntervalMs: 60_000,
    };
    const scenario: SignatureScenario = {
      scenario_id: "scenario-deep",
      namespace: "tenant:payments:replay",
      status: "active",
      session_status: "active",
      rewind_anchor: "2026-07-17T09:55:00Z",
      completed_at: null,
      incident: { slug: "incident-deep", title: "Checkout latency" },
      runs: [
        {
          id: "run-completed-old",
          status: "completed",
          created_at: "2026-07-17T10:00:00Z",
        },
        {
          id: "run-active-new",
          status: "running",
          created_at: "2026-07-17T11:00:00Z",
        },
      ],
      memories: [],
      stages: {},
    };
    const requested: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = new URL(String(input), window.location.origin);
        requested.push(`${url.pathname}${url.search}`);
        if (url.pathname === "/v1/incidents") {
          return Promise.resolve(jsonResponse({ items: [] }));
        }
        if (url.pathname === "/v1/signature-scenarios/scenario-deep") {
          return Promise.resolve(jsonResponse(scenario));
        }
        if (url.pathname === "/v1/namespaces/tenant%3Apayments%3Areplay/beliefs") {
          return Promise.resolve(
            jsonResponse({
              ...historicalSnapshot,
              namespace: scenario.namespace,
              as_of: asOf,
            }),
          );
        }
        return Promise.reject(new Error(`unexpected request: ${url}`));
      }),
    );

    const { result } = renderHook(() => useCockpit());
    await waitFor(() => expect(result.current.loadState).toBe("ready"));

    expect(result.current.scenario?.scenario_id).toBe("scenario-deep");
    expect(result.current.run?.id).toBe("run-active-new");
    expect(result.current.rewindAnchor).toBe("2026-07-17T09:55:00Z");
    expect(result.current.connection).toBe("historical");
    expect(result.current.influenceState).toBe("empty");
    expect(requested).toContain("/v1/signature-scenarios/scenario-deep");
    expect(window.location.search).toContain("scenario_id=scenario-deep");
    expect(window.location.search).toContain("namespace=tenant%3Apayments%3Areplay");
    expect(new URLSearchParams(window.location.search).get("as_of")).toBe(asOf);
  });

  it("rehydrates replay identity on browser history navigation", async () => {
    window.history.replaceState({}, "", "/?scenario_id=scenario-a&namespace=tenant%3Aa");
    window.HINDSIGHT_CONFIG = {
      publicApiBase: "/v1",
      defaultNamespace: "fallback",
      pollIntervalMs: 60_000,
    };
    const scenario = (id: string, namespace: string): SignatureScenario => ({
      scenario_id: id,
      namespace,
      status: "active",
      session_status: "active",
      rewind_anchor: null,
      completed_at: null,
      incident: { slug: `incident-${id}`, title: `Incident ${id}` },
      runs: [{ id: `run-${id}`, status: "queued" }],
      memories: [],
      stages: {},
    });
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = new URL(String(input), window.location.origin);
        if (url.pathname === "/v1/incidents") {
          return Promise.resolve(jsonResponse({ items: [] }));
        }
        if (url.pathname === "/v1/signature-scenarios/scenario-a") {
          return Promise.resolve(jsonResponse(scenario("scenario-a", "tenant:a")));
        }
        if (url.pathname === "/v1/signature-scenarios/scenario-b") {
          return Promise.resolve(jsonResponse(scenario("scenario-b", "tenant:b")));
        }
        if (url.pathname.startsWith("/v1/namespaces/") && url.pathname.endsWith("/beliefs")) {
          const namespace = url.pathname.includes("tenant%3Ab") ? "tenant:b" : "tenant:a";
          return Promise.resolve(jsonResponse({ ...currentSnapshot, namespace }));
        }
        return Promise.reject(new Error(`unexpected request: ${url}`));
      }),
    );

    const { result } = renderHook(() => useCockpit());
    await waitFor(() => expect(result.current.scenario?.scenario_id).toBe("scenario-a"));

    act(() => {
      window.history.pushState(
        {},
        "",
        "/?scenario_id=scenario-b&namespace=tenant%3Ab",
      );
      window.dispatchEvent(new PopStateEvent("popstate"));
    });

    await waitFor(() => expect(result.current.scenario?.scenario_id).toBe("scenario-b"));
    expect(result.current.namespace).toBe("tenant:b");
    expect(result.current.run?.id).toBe("run-scenario-b");
  });

  it("clears stale replay identity when an ordinary incident is selected", async () => {
    window.HINDSIGHT_CONFIG = {
      publicApiBase: "/v1",
      defaultNamespace: currentSnapshot.namespace,
      pollIntervalMs: 60_000,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = new URL(String(input), window.location.origin);
        if (url.pathname === "/v1/signature-scenarios") {
          return Promise.resolve(jsonResponse(approvalScenario()));
        }
        if (url.pathname === "/v1/incidents") {
          return Promise.resolve(
            jsonResponse({ items: [{ slug: "ordinary", title: "Ordinary incident" }] }),
          );
        }
        if (url.pathname === "/v1/incidents/ordinary") {
          return Promise.resolve(
            jsonResponse({
              slug: "ordinary",
              title: "Ordinary incident",
              runs: [{ id: "run-ordinary", status: "running" }],
            }),
          );
        }
        if (url.pathname === "/v1/runs/run-ordinary") {
          return Promise.resolve(
            jsonResponse({
              id: "run-ordinary",
              status: "running",
              namespace: "tenant:ordinary",
            }),
          );
        }
        if (url.pathname === "/v1/namespaces/test%3Ahistory-race/beliefs") {
          return Promise.resolve(jsonResponse(currentSnapshot));
        }
        if (url.pathname === "/v1/namespaces/tenant%3Aordinary/beliefs") {
          return Promise.resolve(
            jsonResponse({ ...currentSnapshot, namespace: "tenant:ordinary" }),
          );
        }
        return Promise.reject(new Error(`unexpected request: ${url}`));
      }),
    );

    const { result } = renderHook(() => useCockpit());
    await waitFor(() => expect(result.current.scenario?.scenario_id).toBe("scenario-approval"));
    expect(new URLSearchParams(window.location.search).get("scenario_id")).toBe(
      "scenario-approval",
    );

    await act(async () => result.current.selectIncident("ordinary"));

    expect(result.current.scenario).toBeNull();
    expect(result.current.run?.id).toBe("run-ordinary");
    expect(result.current.namespace).toBe("tenant:ordinary");
    expect(new URLSearchParams(window.location.search).has("scenario_id")).toBe(false);
    expect(new URLSearchParams(window.location.search).has("as_of")).toBe(false);
  });

  it("distinguishes loading and failed influence reads from an empty ledger", async () => {
    window.history.replaceState({}, "", "/?namespace=tenant%3Aordinary");
    window.HINDSIGHT_CONFIG = {
      publicApiBase: "/v1",
      defaultNamespace: "tenant:ordinary",
      pollIntervalMs: 60_000,
    };
    let resolveInfluence: (response: Response) => void = () => undefined;
    const pendingInfluence = new Promise<Response>((resolve) => {
      resolveInfluence = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = new URL(String(input), window.location.origin);
        if (url.pathname === "/v1/signature-scenarios") {
          return Promise.resolve(
            new Response(JSON.stringify({ detail: "not found" }), {
              status: 404,
              headers: { "content-type": "application/json" },
            }),
          );
        }
        if (url.pathname === "/v1/incidents") {
          return Promise.resolve(jsonResponse({ items: [] }));
        }
        if (url.pathname === "/v1/incidents/ordinary") {
          return Promise.resolve(
            jsonResponse({
              slug: "ordinary",
              title: "Ordinary incident",
              runs: [{ id: "run-ordinary", status: "completed" }],
            }),
          );
        }
        if (url.pathname === "/v1/runs/run-ordinary") {
          return Promise.resolve(
            jsonResponse({
              id: "run-ordinary",
              status: "completed",
              namespace: "tenant:ordinary",
              decision_id: "decision-ordinary",
            }),
          );
        }
        if (url.pathname === "/v1/decisions/decision-ordinary/influence") {
          return pendingInfluence;
        }
        if (url.pathname === "/v1/namespaces/tenant%3Aordinary/beliefs") {
          return Promise.resolve(
            jsonResponse({ ...currentSnapshot, namespace: "tenant:ordinary" }),
          );
        }
        return Promise.reject(new Error(`unexpected request: ${url}`));
      }),
    );

    const { result } = renderHook(() => useCockpit());
    await waitFor(() => expect(result.current.loadState).toBe("ready"));
    await act(async () => result.current.selectIncident("ordinary"));
    expect(result.current.influenceState).toBe("loading");
    expect(result.current.influence).toEqual([]);

    await act(async () => {
      resolveInfluence(
        new Response(JSON.stringify({ detail: "trace projection unavailable" }), {
          status: 503,
          headers: { "content-type": "application/json" },
        }),
      );
      await pendingInfluence;
    });
    await waitFor(() => expect(result.current.influenceState).toBe("error"));
    expect(result.current.influenceError).toBe("trace projection unavailable");
    expect(result.current.influence).toEqual([]);
  });

  it("refreshes the scenario projection after a realtime operation event", async () => {
    window.HINDSIGHT_CONFIG = {
      publicApiBase: "/v1",
      websocketUrl: "wss://socket.example.test/demo",
      defaultNamespace: currentSnapshot.namespace,
    };
    let projectedScenario: SignatureScenario = {
      ...approvalScenario(),
      scenario_id: "scenario-live",
      status: "active",
      runs: [{ id: "run-live", status: "running" }],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = new URL(String(input), window.location.origin);
        if (url.pathname === "/v1/incidents") {
          return Promise.resolve(jsonResponse({ items: [] }));
        }
        if (
          url.pathname === "/v1/signature-scenarios" ||
          url.pathname === "/v1/signature-scenarios/scenario-live"
        ) {
          return Promise.resolve(jsonResponse(projectedScenario));
        }
        if (url.pathname === "/v1/namespaces/test%3Ahistory-race/beliefs") {
          return Promise.resolve(jsonResponse(currentSnapshot));
        }
        if (url.pathname === "/v1/realtime/ticket") {
          return Promise.resolve(jsonResponse({ ticket: "signed-ticket" }));
        }
        return Promise.reject(new Error(`unexpected request: ${url}`));
      }),
    );
    vi.stubGlobal("WebSocket", FakeWebSocket);

    const { result } = renderHook(() => useCockpit());
    await waitFor(() => expect(result.current.scenario?.scenario_id).toBe("scenario-live"));
    await waitFor(() => expect(fakeSocketInstances).toHaveLength(1));
    const socket = fakeSocketInstances[0];
    act(() => socket.open());

    projectedScenario = {
      ...projectedScenario,
      status: "completed",
      completed_at: "2026-07-17T12:05:00Z",
      runs: [{ id: "run-live", status: "completed" }],
    };
    act(() => {
      socket.message(
        realtimeReference("operation-completed", "70.0", "operation"),
      );
    });

    await waitFor(() => expect(result.current.scenario?.status).toBe("completed"));
    expect(result.current.run?.status).toBe("completed");
  });

  it("uses reset scenario identity and its durable rewind anchor", async () => {
    window.HINDSIGHT_CONFIG = {
      publicApiBase: "/v1",
      productApiBase: "/v2",
      defaultNamespace: currentSnapshot.namespace,
      pollIntervalMs: 60_000,
    };
    const resetScenario: SignatureScenario = {
      ...approvalScenario(),
      scenario_id: "scenario-reset",
      namespace: "tenant:reset",
      rewind_anchor: "2026-07-17T12:00:00.123456Z",
    };
    const requested: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), window.location.origin);
        requested.push(url.pathname);
        if (url.pathname === "/v2/me") {
          return Promise.resolve(jsonResponse(effectiveIdentity()));
        }
        if (url.pathname === "/v2/signature-scenarios") {
          return Promise.resolve(jsonResponse(approvalScenario()));
        }
        if (url.pathname === "/v2/demo/poison-rewind/reset" && init?.method === "POST") {
          return Promise.resolve(
            jsonResponse({
              scenario_id: "scenario-reset",
              namespace: "tenant:reset",
              rewind_anchor: "2026-07-17T12:00:00.123456Z",
              incident: resetScenario.incident,
            }),
          );
        }
        if (url.pathname === "/v2/signature-scenarios/scenario-reset") {
          return Promise.resolve(jsonResponse(resetScenario));
        }
        if (url.pathname === "/v2/incidents") {
          return Promise.resolve(jsonResponse({ items: [] }));
        }
        if (url.pathname === "/v2/namespaces/test%3Ahistory-race/beliefs") {
          return Promise.resolve(jsonResponse(currentSnapshot));
        }
        if (url.pathname === "/v2/namespaces/tenant%3Areset/beliefs") {
          return Promise.resolve(
            jsonResponse({ ...currentSnapshot, namespace: "tenant:reset" }),
          );
        }
        return Promise.reject(new Error(`unexpected request: ${url}`));
      }),
    );

    const { result } = renderHook(() => useCockpit({ authAdapter: signedInAdapter() }));
    await waitFor(() => expect(result.current.loadState).toBe("ready"));
    await act(async () => result.current.resetDemo());

    expect(requested).toContain("/v2/signature-scenarios/scenario-reset");
    expect(result.current.scenario?.scenario_id).toBe("scenario-reset");
    expect(result.current.rewindAnchor).toBe("2026-07-17T12:00:00.123456Z");
    expect(new URLSearchParams(window.location.search).get("scenario_id")).toBe(
      "scenario-reset",
    );
  });
});
