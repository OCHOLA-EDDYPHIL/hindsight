import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useCockpit } from "@/hooks/use-cockpit";
import type { Snapshot } from "@/types";

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

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

describe("cockpit historical snapshot selection", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/");
    window.HINDSIGHT_CONFIG = {
      apiBase: "/v1",
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
        if (url.pathname === "/v1/operator/session") {
          return Promise.resolve(jsonResponse({ operator: false }));
        }
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
        if (url.pathname === "/v1/operator/session") {
          return Promise.resolve(jsonResponse({ operator: false }));
        }
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

    let latestSocket: FakeWebSocket | undefined;
    class FakeWebSocket {
      readyState = 1;
      listeners = new Map<string, (event: any) => void>();

      constructor(public url: string) {
        latestSocket = this;
      }

      addEventListener(type: string, listener: (event: any) => void) {
        this.listeners.set(type, listener);
      }

      send() {}

      close() {}
    }
    Object.assign(FakeWebSocket, { OPEN: 1 });
    vi.stubGlobal("WebSocket", FakeWebSocket);

    const { result } = renderHook(() => useCockpit());
    await waitFor(() => expect(result.current.loadState).toBe("ready"));
    await waitFor(() => expect(latestSocket).toBeDefined());
    expect(String(latestSocket?.url)).toContain("ticket=signed-ticket");
    expect(snapshotRequests).toBe(1);

    act(() => {
      latestSocket?.listeners.get("message")?.({
        data: JSON.stringify({
          type: "memory",
          data: { reference: { id: "memory-1", status: "active" } },
        }),
      });
    });

    await waitFor(() => expect(snapshotRequests).toBe(2));
  });
});
