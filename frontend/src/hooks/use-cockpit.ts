import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  publicSnapshotUrl,
  requestProductJson,
  requestPublicJson,
} from "@/lib/api";
import { createHostedUiAuthAdapter, type AuthAdapter } from "@/lib/auth";
import { isoToLocalInput, localInputToIso } from "@/lib/format";
import { BoundedRealtimeTracker, parseRealtimeEnvelopeV2 } from "@/lib/realtime";
import type {
  AuthStatus,
  EffectiveIdentity,
  Incident,
  InfluenceItem,
  MemoryOperation,
  RewindPreview,
  Run,
  RuntimeConfig,
  SignatureScenario,
  Snapshot,
} from "@/types";

const DEFAULT_NAMESPACE = "demo:payments-poison-rewind";
const DEFAULT_REPORT =
  "payments-api checkout p99 latency breached the 2s SLO while processor timeouts and retry fanout rose together.";
const TERMINAL_RUN_STATES = new Set(["completed", "rejected", "failed"]);

type LoadState = "loading" | "ready" | "empty" | "error";
type ConnectionState = "connecting" | "live" | "historical" | "reconnecting" | "disconnected";

interface Notice {
  message: string;
  kind: "status" | "error";
}

interface IncidentListResponse {
  items?: Incident[];
}

interface RunStartResponse {
  run_id: string;
}

interface ResetResponse {
  namespace: string;
  rewind_anchor?: string | null;
  incident?: Incident | null;
}

interface RewindAccepted {
  operation_id: string;
  status?: string;
}

export interface UseCockpitOptions {
  authAdapter?: AuthAdapter | null;
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`Identity response has an invalid ${field}.`);
  }
  return value;
}

export function parseEffectiveIdentity(value: unknown): EffectiveIdentity {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Identity response is invalid.");
  }
  const payload = value as Record<string, unknown>;
  const roles = [payload.token_role, payload.mapped_role, payload.effective_role];
  if (roles.some((role) => role !== "viewer" && role !== "operator")) {
    throw new Error("Identity response has an invalid role.");
  }
  if (
    !Array.isArray(payload.scopes) ||
    payload.scopes.some((scope) => typeof scope !== "string" || !scope.trim())
  ) {
    throw new Error("Identity response has invalid scopes.");
  }
  const scopes = [...new Set(payload.scopes as string[])];
  const expectedRole =
    payload.token_role === "operator" && payload.mapped_role === "operator"
      ? "operator"
      : "viewer";
  if (
    payload.effective_role !== expectedRole ||
    !scopes.includes("read") ||
    !scopes.includes("realtime") ||
    scopes.includes("write") !== (expectedRole === "operator")
  ) {
    throw new Error("Identity response contains conflicting effective access.");
  }
  if (
    typeof payload.expires_at !== "number" ||
    !Number.isSafeInteger(payload.expires_at) ||
    payload.expires_at <= Math.floor(Date.now() / 1000)
  ) {
    throw new Error("Identity response is expired or invalid.");
  }
  return {
    principal_id: requiredString(payload.principal_id, "principal ID"),
    tenant_id: requiredString(payload.tenant_id, "tenant ID"),
    tenant_slug: requiredString(payload.tenant_slug, "tenant slug"),
    token_role: payload.token_role as EffectiveIdentity["token_role"],
    mapped_role: payload.mapped_role as EffectiveIdentity["mapped_role"],
    effective_role: payload.effective_role as EffectiveIdentity["effective_role"],
    scopes,
    expires_at: payload.expires_at,
  };
}

function initialNamespace(config: RuntimeConfig): string {
  return (
    new URLSearchParams(window.location.search).get("namespace") ||
    config.defaultNamespace ||
    DEFAULT_NAMESPACE
  );
}

export function useCockpit(options: UseCockpitOptions = {}) {
  const config = useRef<RuntimeConfig>(window.HINDSIGHT_CONFIG || {}).current;
  const authSelection = useRef<{ adapter: AuthAdapter | null; error: string | null }>();
  if (!authSelection.current) {
    try {
      authSelection.current = {
        adapter: Object.prototype.hasOwnProperty.call(options, "authAdapter")
          ? options.authAdapter || null
          : createHostedUiAuthAdapter(config.auth),
        error: null,
      };
    } catch (error) {
      authSelection.current = { adapter: null, error: (error as Error).message };
    }
  }
  const authAdapter = authSelection.current.adapter;
  const params = useRef(new URLSearchParams(window.location.search)).current;
  const explicitNamespace = params.has("namespace");

  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [loadError, setLoadError] = useState("");
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [scenario, setScenario] = useState<SignatureScenario | null>(null);
  const [namespace, setNamespaceValue] = useState(() => initialNamespace(config));
  const namespaceRef = useRef(namespace);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const snapshotRef = useRef<Snapshot | null>(null);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [incident, setIncident] = useState<Incident | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const runRef = useRef<Run | null>(null);
  const [influence, setInfluence] = useState<InfluenceItem[]>([]);
  const [authStatus, setAuthStatus] = useState<AuthStatus>("initializing");
  const authStatusRef = useRef<AuthStatus>("initializing");
  const [identity, setIdentity] = useState<EffectiveIdentity | null>(null);
  const identityRef = useRef<EffectiveIdentity | null>(null);
  const [authEpoch, setAuthEpoch] = useState(0);
  const [notice, setNotice] = useState<Notice | null>(null);
  const noticeTimer = useRef<number>();
  const snapshotRefreshTimer = useRef<number>();
  const [incidentInput, setIncidentInput] = useState(DEFAULT_REPORT);
  const [rewindAnchor, setRewindAnchor] = useState<string | null>(null);
  const [rewindTimestamp, setRewindTimestamp] = useState("");
  const [rewindReason, setRewindReason] = useState(
    "Stale guidance led to an unsafe recommendation",
  );
  const [rewindPreview, setRewindPreview] = useState<RewindPreview | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const activeRunId = useRef<string | null>(null);
  const snapshotView = useRef<"live" | "historical">("live");
  const snapshotRequest = useRef(0);
  const incidentRequest = useRef(0);
  const runRequest = useRef(0);
  const influenceRequest = useRef(0);
  const realtimeTracker = useRef(new BoundedRealtimeTracker());
  const initialized = useRef(false);

  const updateNamespace = useCallback((value: string, updateUrl = true) => {
    namespaceRef.current = value;
    setNamespaceValue(value);
    if (updateUrl) {
      const url = new URL(window.location.href);
      url.searchParams.set("namespace", value);
      window.history.replaceState({}, "", url);
    }
  }, []);

  const announce = useCallback((message: string, kind: Notice["kind"] = "status") => {
    window.clearTimeout(noticeTimer.current);
    setNotice({ message, kind });
    noticeTimer.current = window.setTimeout(() => setNotice(null), 6000);
  }, []);

  const moveToPublicSurface = useCallback(() => {
    identityRef.current = null;
    authStatusRef.current = "public";
    setIdentity(null);
    setAuthStatus("public");
    setAuthEpoch((value) => value + 1);
  }, []);

  const readJson = useCallback(
    async <T,>(path: string, options: RequestInit = {}): Promise<T> => {
      if (authStatusRef.current === "authenticated") {
        const token = authAdapter?.accessToken() || null;
        if (token) {
          try {
            return await requestProductJson<T>(config, path, token, options);
          } catch (error) {
            if (!(error instanceof ApiError && error.status === 401)) throw error;
            authAdapter?.clear();
            moveToPublicSurface();
            announce("Your sign-in expired. The public read-only replay is still available.", "error");
          }
        } else {
          authAdapter?.clear();
          moveToPublicSurface();
          announce("Your sign-in expired. The public read-only replay is still available.", "error");
        }
      }
      return requestPublicJson<T>(config, path, options);
    },
    [announce, authAdapter, config, moveToPublicSurface],
  );

  const requireWriteAccess = useCallback((): string | null => {
    const authorized =
      authStatusRef.current === "authenticated" &&
      identityRef.current?.effective_role === "operator" &&
      identityRef.current.scopes.includes("write");
    const token = authorized ? authAdapter?.accessToken() || null : null;
    if (authorized && token) return token;
    if (authorized && !token) {
      authAdapter?.clear();
      moveToPublicSurface();
    }
    announce("Operator authorization with write scope is required.", "error");
    return null;
  }, [announce, authAdapter, moveToPublicSurface]);

  const productWriteJson = useCallback(
    async <T,>(path: string, token: string, options: RequestInit = {}): Promise<T> => {
      try {
        return await requestProductJson<T>(config, path, token, options);
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          authAdapter?.clear();
          moveToPublicSurface();
          announce("Your sign-in expired. The public read-only replay is still available.", "error");
        }
        throw error;
      }
    },
    [announce, authAdapter, config, moveToPublicSurface],
  );

  const applySnapshot = useCallback((value: Snapshot) => {
    snapshotRef.current = value;
    setSnapshot(value);
  }, []);

  const loadSnapshot = useCallback(
    async (asOf?: string | null, targetNamespace?: string) => {
      const requestedView = asOf ? "historical" : "live";
      snapshotView.current = requestedView;
      const requestId = ++snapshotRequest.current;
      const requestedNamespace = targetNamespace || namespaceRef.current;
      try {
        let next: Snapshot;
        if (authStatusRef.current === "authenticated" || !config.snapshotBase) {
          const query = asOf ? `?as_of=${encodeURIComponent(asOf)}` : "";
          next = await readJson<Snapshot>(
            `/namespaces/${encodeURIComponent(requestedNamespace)}/beliefs${query}`,
          );
        } else {
          const response = await fetch(publicSnapshotUrl(config, requestedNamespace, asOf), {
            credentials: "omit",
            headers: { accept: "application/json" },
          });
          if (!response.ok) throw new Error(await response.text());
          next = (await response.json()) as Snapshot;
        }
        if (requestId !== snapshotRequest.current || requestedNamespace !== namespaceRef.current) {
          return;
        }
        applySnapshot(next);
        snapshotView.current = next.as_of ? "historical" : requestedView;
        setConnection(asOf ? "historical" : "live");
      } catch (error) {
        if (requestId !== snapshotRequest.current) return;
        snapshotView.current = snapshotRef.current?.as_of ? "historical" : "live";
        setConnection("disconnected");
        throw error;
      }
    },
    [applySnapshot, config, readJson],
  );

  const scheduleSnapshotRefresh = useCallback(
    (delay = 100) => {
      if (snapshotView.current !== "live") return;
      const scheduledNamespace = namespaceRef.current;
      const requestFence = ++snapshotRequest.current;
      window.clearTimeout(snapshotRefreshTimer.current);
      snapshotRefreshTimer.current = window.setTimeout(() => {
        if (
          snapshotView.current !== "live" ||
          namespaceRef.current !== scheduledNamespace ||
          snapshotRequest.current !== requestFence
        ) {
          return;
        }
        void loadSnapshot(null, scheduledNamespace).catch(() => undefined);
      }, delay);
    },
    [loadSnapshot],
  );

  const loadInfluence = useCallback(
    async (decisionId: string, expectedRunId?: string) => {
      const requestId = ++influenceRequest.current;
      try {
        const payload = await readJson<{ memories?: InfluenceItem[] }>(
          `/decisions/${encodeURIComponent(decisionId)}/influence`,
        );
        if (requestId !== influenceRequest.current) return;
        if (expectedRunId && expectedRunId !== activeRunId.current) return;
        setInfluence(payload.memories || []);
      } catch (error) {
        if (requestId !== influenceRequest.current) return;
        setInfluence([]);
        announce(`Decision influence could not be loaded: ${(error as Error).message}`, "error");
      }
    },
    [announce, readJson],
  );

  const loadRun = useCallback(
    async (runId: string, poll = false) => {
      if (activeRunId.current && runId !== activeRunId.current) return;
      const requestId = ++runRequest.current;
      try {
        const next = await readJson<Run>(`/runs/${encodeURIComponent(runId)}`);
        if (requestId !== runRequest.current || runId !== activeRunId.current) return;
        runRef.current = next;
        setRun(next);
        if (next.decision_id) void loadInfluence(next.decision_id, runId);
        if (TERMINAL_RUN_STATES.has(next.status) && snapshotView.current === "live") {
          await loadSnapshot(null).catch(() => undefined);
        } else if (poll && next.status !== "awaiting_approval") {
          window.setTimeout(() => void loadRun(runId, true), 1400);
        }
      } catch (error) {
        if (requestId !== runRequest.current) return;
        announce(`Run status could not be loaded: ${(error as Error).message}`, "error");
      }
    },
    [announce, loadInfluence, loadSnapshot, readJson],
  );

  const selectIncident = useCallback(
    async (slug: string) => {
      if (!slug) return;
      const requestId = ++incidentRequest.current;
      activeRunId.current = null;
      try {
        const nextIncident = await readJson<Incident>(
          `/incidents/${encodeURIComponent(slug)}`,
        );
        if (requestId !== incidentRequest.current) return;
        const latest = nextIncident.runs?.[0];
        const nextRun = latest
          ? await readJson<Run>(`/runs/${encodeURIComponent(latest.id)}`)
          : null;
        if (requestId !== incidentRequest.current) return;
        setIncident(nextIncident);
        setRun(nextRun);
        runRef.current = nextRun;
        activeRunId.current = nextRun?.id || null;
        if (nextRun?.namespace) updateNamespace(nextRun.namespace);
        setIncidentInput(nextRun?.user_input || nextIncident.summary || DEFAULT_REPORT);
        await loadSnapshot(null, nextRun?.namespace || namespaceRef.current);
        if (nextRun?.decision_id) void loadInfluence(nextRun.decision_id, nextRun.id);
        else setInfluence([]);
      } catch (error) {
        if (requestId !== incidentRequest.current) return;
        announce(`Incident could not be loaded: ${(error as Error).message}`, "error");
      }
    },
    [announce, loadInfluence, loadSnapshot, readJson, updateNamespace],
  );

  const loadIncidents = useCallback(
    async (preferredSlug?: string | null, select = true) => {
      try {
        const payload = await readJson<IncidentListResponse>("/incidents");
        const items = payload.items || [];
        setIncidents(items);
        if (!select) return;
        const selected =
          items.find((item) => item.slug === preferredSlug) ||
          items.find(
            (item) =>
              item.slug.startsWith("demo-payments-checkout-latency:") &&
              item.latest_run_status === "completed",
          ) ||
          items.find((item) => item.slug === "demo-payments-checkout-latency") ||
          items[0];
        if (selected) await selectIncident(selected.slug);
      } catch {
        setIncidents([]);
      }
    },
    [readJson, selectIncident],
  );

  const loadScenario = useCallback(
    async (selector?: { namespace?: string; decisionId?: string }) => {
      const query = new URLSearchParams();
      if (selector?.namespace) query.set("namespace", selector.namespace);
      if (selector?.decisionId) query.set("decision_id", selector.decisionId);
      const suffix = query.size ? `?${query}` : "";
      const next = await readJson<SignatureScenario>(`/signature-scenarios${suffix}`);
      setScenario(next);
      setIncident(next.incident || null);
      const preferredRun = [...next.runs].reverse().find((item) => item.status === "completed") ||
        [...next.runs].reverse().find((item) => item.status === "rejected") ||
        next.runs.at(-1) ||
        null;
      setRun(preferredRun);
      runRef.current = preferredRun;
      activeRunId.current = preferredRun?.id || null;
      setInfluence(
        (preferredRun?.trace?.reads || []).map((read) => ({
          status: read.memory_status || undefined,
          read: {
            id: read.id,
            rank: read.rank,
            distance: read.distance,
          },
          memory: {
            id: read.memory_id,
            belief_id: read.belief_id,
            version_number: read.version_number,
            status: read.memory_status,
          },
        })),
      );
      updateNamespace(next.namespace, Boolean(selector));
      await loadSnapshot(null, next.namespace).catch(() => undefined);
      setLoadState("ready");
      return next;
    },
    [loadSnapshot, readJson, updateNamespace],
  );

  const initializeAuth = useCallback(async () => {
    if (authSelection.current?.error) {
      moveToPublicSurface();
      announce(`Sign-in configuration is invalid: ${authSelection.current.error}`, "error");
      return;
    }
    if (!authAdapter) {
      moveToPublicSurface();
      return;
    }
    try {
      const session = await authAdapter.initialize();
      if (!session) {
        moveToPublicSurface();
        return;
      }
      const payload = await requestProductJson<unknown>(config, "/me", session.accessToken);
      const resolvedIdentity = parseEffectiveIdentity(payload);
      identityRef.current = resolvedIdentity;
      authStatusRef.current = "authenticated";
      setIdentity(resolvedIdentity);
      setAuthStatus("authenticated");
      setAuthEpoch((value) => value + 1);
    } catch (error) {
      authAdapter.clear();
      moveToPublicSurface();
      announce(`Sign-in could not be established: ${(error as Error).message}`, "error");
    }
  }, [announce, authAdapter, config, moveToPublicSurface]);

  const retryInitialLoad = useCallback(async () => {
    setLoadState("loading");
    setLoadError("");
    try {
      if (
        authStatusRef.current !== "authenticated" &&
        config.snapshotBase &&
        !explicitNamespace
      ) {
        await loadSnapshot(params.get("as_of"), namespaceRef.current);
        await loadIncidents(null, false);
        setLoadState("ready");
      } else if (explicitNamespace) {
        await loadSnapshot(params.get("as_of"), namespaceRef.current);
        await loadIncidents(null, false);
        try {
          await loadScenario({ namespace: namespaceRef.current });
        } catch (error) {
          if (!(error instanceof ApiError && error.status === 404)) throw error;
          setLoadState("ready");
        }
      } else {
        try {
          await loadScenario();
        } catch (error) {
          if (!(error instanceof ApiError && error.status === 404)) throw error;
          setScenario(null);
          setLoadState("empty");
          await loadSnapshot(null, namespaceRef.current).catch(() => undefined);
        }
      }
    } catch (error) {
      setLoadError((error as Error).message);
      setLoadState("error");
      setConnection("disconnected");
    }
  }, [config.snapshotBase, explicitNamespace, loadIncidents, loadScenario, loadSnapshot, params]);

  useEffect(() => {
    if (initialized.current) return;
    initialized.current = true;
    void (async () => {
      await initializeAuth();
      await retryInitialLoad();
    })();
  }, [initializeAuth, retryInitialLoad]);

  const handleLiveEvent = useCallback(
    (payload: unknown) => {
      const envelope = parseRealtimeEnvelopeV2(payload);
      const raw = payload as Record<string, any>;
      const eventNamespace = envelope?.namespace || raw.namespace;
      if (eventNamespace && eventNamespace !== namespaceRef.current) return;
      if (snapshotView.current !== "live") return;
      if (envelope) {
        const disposition = realtimeTracker.current.observe(namespaceRef.current, envelope);
        if (disposition === "duplicate") return;
        if (["memory", "operation"].includes(envelope.type)) {
          scheduleSnapshotRefresh();
          return;
        }
        const reference = envelope.data.reference as Record<string, unknown> | undefined;
        const runId = envelope.run_id ||
          (typeof reference?.run_id === "string" ? reference.run_id : null);
        if (runId) void loadRun(runId);
        return;
      }

      const type = raw.type || raw.event;
      const data = raw.data || raw;
      if (["memory", "operation"].includes(type) && data.reference) {
        scheduleSnapshotRefresh();
        return;
      }
      if (type === "memory" && data.memory) {
        const previous = snapshotRef.current;
        if (previous && !previous.as_of) {
          const memories = new Map(previous.memories.map((memory) => [memory.id, memory]));
          memories.set(data.memory.id, data.memory);
          const timeline = new Set(previous.timeline);
          [
            data.memory.t_valid,
            data.memory.written_at,
            data.memory.t_invalid,
            data.memory.invalidated_at,
          ]
            .filter(Boolean)
            .forEach((value) => timeline.add(value));
          applySnapshot({
            ...previous,
            memories: [...memories.values()],
            timeline: [...timeline].sort(),
          });
        }
      }
      if (type === "operation" && data.operation) {
        const previous = snapshotRef.current;
        if (previous && !previous.as_of) {
          const operations = new Map(previous.operations.map((item) => [item.id, item]));
          operations.set(data.operation.id, data.operation);
          applySnapshot({ ...previous, operations: [...operations.values()] });
        }
      }
      if (["run", "run_event"].includes(type)) {
        const runId = raw.run_id || data.run_id;
        if (runId) void loadRun(runId);
      }
    },
    [applySnapshot, loadRun, scheduleSnapshotRefresh],
  );

  const subscribeSocket = useCallback((targetNamespace = namespaceRef.current) => {
    const socket = socketRef.current;
    if (socket?.readyState !== WebSocket.OPEN) return;
    socket.send(
      JSON.stringify({
        type: "subscribe",
        namespace: targetNamespace,
        run_id: runRef.current?.id || null,
      }),
    );
  }, []);

  useEffect(() => {
    if (!initialized.current || authStatus === "initializing") return;
    let disposed = false;
    let reconnectTimer: number | undefined;
    let interval: number | undefined;

    if (config.websocketUrl) {
      const connect = async () => {
        if (disposed) return;
        try {
          const ticket = await readJson<{ ticket: string }>("/realtime/ticket", {
            method: "POST",
          });
          if (disposed) return;
          const url = new URL(config.websocketUrl as string);
          url.searchParams.set("ticket", ticket.ticket);
          const socket = new WebSocket(url);
          socketRef.current = socket;
          socket.addEventListener("open", () => {
            if (disposed || socketRef.current !== socket) return;
            subscribeSocket(namespaceRef.current);
            if (snapshotView.current === "live") {
              setConnection("live");
              scheduleSnapshotRefresh(0);
              const active = runRef.current;
              if (active) void loadRun(active.id);
            }
          });
          socket.addEventListener("message", (event) => {
            if (disposed || socketRef.current !== socket) return;
            try {
              handleLiveEvent(JSON.parse(event.data));
            } catch {
              announce("A live update could not be decoded.", "error");
            }
          });
          socket.addEventListener("close", () => {
            if (disposed || socketRef.current !== socket) return;
            if (snapshotView.current === "live") setConnection("reconnecting");
            socketRef.current = null;
            reconnectTimer = window.setTimeout(() => void connect(), 1600);
          });
          socket.addEventListener("error", () => {
            if (socketRef.current === socket) socket.close();
          });
        } catch {
          if (disposed) return;
          if (snapshotView.current === "live") setConnection("reconnecting");
          reconnectTimer = window.setTimeout(() => void connect(), 1600);
        }
      };
      void connect();
    } else {
      const pollMs = Math.max(1500, Number(config.pollIntervalMs || 4000));
      interval = window.setInterval(() => {
        if (snapshotView.current === "live") {
          void loadSnapshot(null).catch(() => undefined);
          const active = runRef.current;
          if (active && !TERMINAL_RUN_STATES.has(active.status)) void loadRun(active.id);
        }
      }, pollMs);
    }

    return () => {
      disposed = true;
      window.clearTimeout(reconnectTimer);
      window.clearTimeout(snapshotRefreshTimer.current);
      window.clearInterval(interval);
      if (socketRef.current) {
        const socket = socketRef.current;
        socketRef.current = null;
        socket.close();
      }
    };
  }, [
    announce,
    authEpoch,
    authStatus,
    config,
    handleLiveEvent,
    loadRun,
    loadSnapshot,
    namespace,
    readJson,
    scheduleSnapshotRefresh,
    subscribeSocket,
  ]);

  const signIn = useCallback(async () => {
    if (!authAdapter) {
      announce("Hosted sign-in is not configured for this deployment.", "error");
      return;
    }
    try {
      await authAdapter.signIn(
        `${window.location.pathname}${window.location.search}${window.location.hash}`,
      );
    } catch (error) {
      announce(`Sign-in could not start: ${(error as Error).message}`, "error");
    }
  }, [announce, authAdapter]);

  const signOut = useCallback(() => {
    snapshotRequest.current += 1;
    incidentRequest.current += 1;
    runRequest.current += 1;
    influenceRequest.current += 1;
    activeRunId.current = null;
    realtimeTracker.current = new BoundedRealtimeTracker();
    if (socketRef.current) {
      const socket = socketRef.current;
      socketRef.current = null;
      socket.close();
    }
    setScenario(null);
    setIncidents([]);
    setIncident(null);
    setRun(null);
    runRef.current = null;
    setInfluence([]);
    applySnapshot({
      mode: "current",
      namespace: namespaceRef.current,
      memories: [],
      operations: [],
      timeline: [],
    });
    moveToPublicSurface();
    authAdapter?.signOut();
    announce("Signed out. The public read-only replay remains available.");
    void retryInitialLoad();
  }, [announce, applySnapshot, authAdapter, moveToPublicSurface, retryInitialLoad]);

  const resetDemo = useCallback(async () => {
    const token = requireWriteAccess();
    if (!token) return;
    setBusy("reset");
    try {
      const payload = await productWriteJson<ResetResponse>("/demo/poison-rewind/reset", token, {
        method: "POST",
        body: JSON.stringify({ namespace: namespaceRef.current }),
      });
      updateNamespace(payload.namespace);
      subscribeSocket(payload.namespace);
      setRewindAnchor(payload.rewind_anchor || null);
      setRewindTimestamp(isoToLocalInput(payload.rewind_anchor));
      setRewindPreview(null);
      await loadIncidents(payload.incident?.slug, false);
      await loadScenario({ namespace: payload.namespace });
      setLoadState("ready");
      announce("Known-good payment memory restored. The replay is ready.");
    } catch (error) {
      announce(`Demo reset failed: ${(error as Error).message}`, "error");
    } finally {
      setBusy(null);
    }
  }, [
    announce,
    loadIncidents,
    loadScenario,
    productWriteJson,
    requireWriteAccess,
    subscribeSocket,
    updateNamespace,
  ]);

  const poisonDemo = useCallback(async () => {
    const token = requireWriteAccess();
    if (!token) return;
    if (!rewindAnchor) {
      announce("Reset the replay before importing stale guidance.", "error");
      return;
    }
    setBusy("poison");
    try {
      await productWriteJson("/demo/poison-rewind/poison", token, {
        method: "POST",
        body: JSON.stringify({ namespace: namespaceRef.current }),
      });
      await loadScenario({ namespace: namespaceRef.current });
      announce("Stale retry-amplifying guidance imported with provenance.");
    } catch (error) {
      announce(`Guidance import failed: ${(error as Error).message}`, "error");
    } finally {
      setBusy(null);
    }
  }, [announce, loadScenario, productWriteJson, requireWriteAccess, rewindAnchor]);

  const startRun = useCallback(async () => {
    const token = requireWriteAccess();
    if (!token) return;
    if (!incident) {
      announce("Choose or reset a signature incident first.", "error");
      return;
    }
    setBusy("run");
    try {
      const result = await productWriteJson<RunStartResponse>(
        `/incidents/${encodeURIComponent(incident.slug)}/runs`,
        token,
        {
          method: "POST",
          headers: { "Idempotency-Key": crypto.randomUUID() },
          body: JSON.stringify({
            namespace: namespaceRef.current,
            user_input: incidentInput.trim(),
          }),
        },
      );
      activeRunId.current = result.run_id;
      announce("Agent run queued. Decision phases will update here.");
      await loadRun(result.run_id, true);
    } catch (error) {
      announce(`Run could not start: ${(error as Error).message}`, "error");
    } finally {
      setBusy(null);
    }
  }, [announce, incident, incidentInput, loadRun, productWriteJson, requireWriteAccess]);

  const decideRun = useCallback(
    async (approved: boolean) => {
      const token = requireWriteAccess();
      if (!token) return;
      const current = runRef.current;
      if (!current) return;
      const recommendationId = current.action_trace?.recommendation?.id?.trim();
      const selectionFingerprint = current.action_trace?.selection?.fingerprint?.trim();
      if (!recommendationId || !selectionFingerprint) {
        announce(
          "Approval identity is unavailable. Refresh or rerun the analysis before deciding.",
          "error",
        );
        return;
      }
      setBusy(approved ? "approve" : "reject");
      try {
        await productWriteJson(`/runs/${encodeURIComponent(current.id)}/approval`, token, {
          method: "POST",
          body: JSON.stringify({
            approved,
            recommendation_id: recommendationId,
            selection_fingerprint: selectionFingerprint,
          }),
        });
        announce(
          approved
            ? "Recommendation approved and retained in the audit trail."
            : "Recommendation rejected and retained in the audit trail.",
        );
        await loadRun(current.id, true);
      } catch (error) {
        announce(`Decision could not be recorded: ${(error as Error).message}`, "error");
      } finally {
        setBusy(null);
      }
    },
    [announce, loadRun, productWriteJson, requireWriteAccess],
  );

  const invalidatePreview = useCallback(() => setRewindPreview(null), []);

  const previewRewind = useCallback(async () => {
    const token = requireWriteAccess();
    if (!token) return;
    const target = rewindAnchor || localInputToIso(rewindTimestamp);
    if (!target) {
      announce("Choose a valid rewind timestamp.", "error");
      return;
    }
    setBusy("preview");
    try {
      const preview = await productWriteJson<RewindPreview>(
        `/namespaces/${encodeURIComponent(namespaceRef.current)}/rewinds/preview`,
        token,
        {
          method: "POST",
          body: JSON.stringify({
            target_timestamp: target,
            reason: rewindReason.trim() || "Operator-requested rewind",
          }),
        },
      );
      setRewindPreview(preview);
    } catch (error) {
      setRewindPreview(null);
      announce(`Rewind preview failed: ${(error as Error).message}`, "error");
    } finally {
      setBusy(null);
    }
  }, [
    announce,
    productWriteJson,
    requireWriteAccess,
    rewindAnchor,
    rewindReason,
    rewindTimestamp,
  ]);

  const waitForOperation = useCallback(
    async (operationId: string) => {
      const pollSeconds = Math.max(60, Number(config.operationPollSeconds || 600));
      const deadline = Date.now() + pollSeconds * 1000;
      let lastOperation: MemoryOperation | null = null;
      while (Date.now() < deadline) {
        const operation = await readJson<MemoryOperation>(
          `/memory/operations/${encodeURIComponent(operationId)}`,
        );
        lastOperation = operation;
        if (["completed", "conflict", "failed"].includes(operation.status)) {
          return operation;
        }
        const previous = snapshotRef.current;
        if (previous) {
          const operations = new Map(previous.operations.map((item) => [item.id, item]));
          operations.set(operation.id, operation);
          applySnapshot({ ...previous, operations: [...operations.values()] });
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
      }
      const detail = lastOperation?.failure_detail ? `: ${lastOperation.failure_detail}` : "";
      throw new Error(
        `operation did not reach a terminal state; last status ${lastOperation?.status || "unknown"}${detail}`,
      );
    },
    [applySnapshot, config.operationPollSeconds, readJson],
  );

  const executeRewind = useCallback(async () => {
    const token = requireWriteAccess();
    if (!token) return;
    if (!rewindPreview) return;
    setBusy("execute");
    try {
      const accepted = await productWriteJson<RewindAccepted>(
        `/namespaces/${encodeURIComponent(namespaceRef.current)}/rewinds`,
        token,
        {
          method: "POST",
          headers: { "Idempotency-Key": crypto.randomUUID() },
          body: JSON.stringify({
            preview_id: rewindPreview.id,
            fingerprint: rewindPreview.fingerprint,
          }),
        },
      );
      const queued: MemoryOperation = {
        id: accepted.operation_id,
        operation_type: "rewind",
        status: accepted.status || "queued",
        reason: rewindReason.trim() || "Operator-requested rewind",
        invalidated_memory_ids: [],
        restored_memory_ids: [],
        created_at: new Date().toISOString(),
      };
      const previous = snapshotRef.current;
      if (previous) applySnapshot({ ...previous, operations: [...previous.operations, queued] });
      setRewindPreview(null);
      announce("Rewind queued. The approved preview is being verified.");
      const operation = await waitForOperation(accepted.operation_id);
      await loadSnapshot();
      if (operation.status === "completed") {
        announce("Belief state rewound. Historical versions remain available for audit.");
      } else {
        announce(
          `Rewind ended in ${operation.status}: ${operation.failure_detail || "state changed"}`,
          "error",
        );
      }
    } catch (error) {
      announce(`Rewind failed: ${(error as Error).message}`, "error");
    } finally {
      setBusy(null);
    }
  }, [
    announce,
    applySnapshot,
    loadSnapshot,
    productWriteJson,
    requireWriteAccess,
    rewindPreview,
    rewindReason,
    waitForOperation,
  ]);

  const selectHistorical = useCallback(
    async (asOf?: string | null) => {
      try {
        await loadSnapshot(asOf || null);
      } catch (error) {
        announce(`Belief state could not be loaded: ${(error as Error).message}`, "error");
      }
    },
    [announce, loadSnapshot],
  );

  const canWrite =
    authStatus === "authenticated" &&
    identity?.effective_role === "operator" &&
    identity.scopes.includes("write");

  return {
    config,
    authConfigured: Boolean(authAdapter),
    authStatus,
    identity,
    canWrite,
    explicitNamespace,
    loadState,
    loadError,
    connection,
    scenario,
    namespace,
    snapshot,
    incidents,
    incident,
    run,
    influence,
    notice,
    incidentInput,
    rewindAnchor,
    rewindTimestamp,
    rewindReason,
    rewindPreview,
    busy,
    retryInitialLoad,
    signIn,
    signOut,
    selectIncident,
    setIncidentInput,
    resetDemo,
    poisonDemo,
    startRun,
    decideRun,
    setRewindTimestamp: (value: string) => {
      setRewindAnchor(null);
      setRewindTimestamp(value);
      invalidatePreview();
    },
    setRewindReason: (value: string) => {
      setRewindReason(value);
      invalidatePreview();
    },
    previewRewind,
    executeRewind,
    selectHistorical,
  };
}
