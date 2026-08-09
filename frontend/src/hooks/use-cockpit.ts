import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, requestJson, snapshotUrl } from "@/lib/api";
import { isoToLocalInput, localInputToIso } from "@/lib/format";
import type {
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

function initialNamespace(config: RuntimeConfig): string {
  return (
    new URLSearchParams(window.location.search).get("namespace") ||
    config.defaultNamespace ||
    DEFAULT_NAMESPACE
  );
}

export function useCockpit() {
  const config = useRef<RuntimeConfig>(window.HINDSIGHT_CONFIG || {}).current;
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
  const [operator, setOperator] = useState(false);
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
        const response = await fetch(snapshotUrl(config, requestedNamespace, asOf), {
          credentials: "include",
        });
        if (!response.ok) throw new Error(await response.text());
        const next = (await response.json()) as Snapshot;
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
    [applySnapshot, config],
  );

  const loadInfluence = useCallback(
    async (decisionId: string, expectedRunId?: string) => {
      const requestId = ++influenceRequest.current;
      try {
        const payload = await requestJson<{ memories?: InfluenceItem[] }>(
          config,
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
    [announce, config],
  );

  const loadRun = useCallback(
    async (runId: string, poll = false) => {
      if (activeRunId.current && runId !== activeRunId.current) return;
      const requestId = ++runRequest.current;
      try {
        const next = await requestJson<Run>(config, `/runs/${encodeURIComponent(runId)}`);
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
    [announce, config, loadInfluence, loadSnapshot],
  );

  const selectIncident = useCallback(
    async (slug: string) => {
      if (!slug) return;
      const requestId = ++incidentRequest.current;
      activeRunId.current = null;
      try {
        const nextIncident = await requestJson<Incident>(
          config,
          `/incidents/${encodeURIComponent(slug)}`,
        );
        if (requestId !== incidentRequest.current) return;
        const latest = nextIncident.runs?.[0];
        const nextRun = latest
          ? await requestJson<Run>(config, `/runs/${encodeURIComponent(latest.id)}`)
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
    [announce, config, loadInfluence, loadSnapshot, updateNamespace],
  );

  const loadIncidents = useCallback(
    async (preferredSlug?: string | null, select = true) => {
      try {
        const payload = await requestJson<IncidentListResponse>(config, "/incidents");
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
    [config, selectIncident],
  );

  const loadScenario = useCallback(
    async (selector?: { namespace?: string; decisionId?: string }) => {
      const query = new URLSearchParams();
      if (selector?.namespace) query.set("namespace", selector.namespace);
      if (selector?.decisionId) query.set("decision_id", selector.decisionId);
      const suffix = query.size ? `?${query}` : "";
      const next = await requestJson<SignatureScenario>(
        config,
        `/signature-scenarios${suffix}`,
      );
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
    [config, loadSnapshot, updateNamespace],
  );

  const establishOperatorSession = useCallback(async () => {
    const hash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    const fragmentToken = hash.get("operator");
    if (fragmentToken) {
      try {
        await requestJson(config, "/operator/session", {
          method: "POST",
          body: JSON.stringify({ token: fragmentToken }),
        });
        window.history.replaceState(null, "", `${location.pathname}${location.search}`);
      } catch (error) {
        announce(`Operator unlock failed: ${(error as Error).message}`, "error");
      }
    }
    try {
      const session = await requestJson<{ operator: boolean }>(config, "/operator/session");
      setOperator(Boolean(session.operator));
    } catch {
      setOperator(false);
    }
  }, [announce, config]);

  const retryInitialLoad = useCallback(async () => {
    setLoadState("loading");
    setLoadError("");
    try {
      if (config.snapshotBase && !explicitNamespace) {
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
      await establishOperatorSession();
      await retryInitialLoad();
    })();
  }, [establishOperatorSession, retryInitialLoad]);

  const handleLiveEvent = useCallback(
    (payload: Record<string, any>) => {
      if (payload.namespace && payload.namespace !== namespaceRef.current) return;
      if (snapshotView.current !== "live") return;
      const type = payload.type || payload.event;
      const data = payload.data || payload;
      if (["memory", "operation"].includes(type) && data.reference) {
        window.clearTimeout(snapshotRefreshTimer.current);
        snapshotRefreshTimer.current = window.setTimeout(
          () => void loadSnapshot(null).catch(() => undefined),
          100,
        );
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
        const runId = payload.run_id || data.run_id;
        if (runId) void loadRun(runId);
      }
    },
    [applySnapshot, loadRun, loadSnapshot],
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
    if (!initialized.current) return;
    let disposed = false;
    let reconnectTimer: number | undefined;
    let interval: number | undefined;

    if (config.websocketUrl) {
      const connect = async () => {
        if (disposed) return;
        try {
          const ticket = await requestJson<{ ticket: string }>(config, "/realtime/ticket", {
            method: "POST",
          });
          if (disposed) return;
          const url = new URL(config.websocketUrl as string);
          url.searchParams.set("ticket", ticket.ticket);
          const socket = new WebSocket(url);
          socketRef.current = socket;
          socket.addEventListener("open", () => {
            if (disposed) return;
            if (snapshotView.current === "live") setConnection("live");
            subscribeSocket(namespaceRef.current);
          });
          socket.addEventListener("message", (event) => {
            try {
              handleLiveEvent(JSON.parse(event.data));
            } catch {
              announce("A live update could not be decoded.", "error");
            }
          });
          socket.addEventListener("close", () => {
            if (disposed) return;
            if (snapshotView.current === "live") setConnection("reconnecting");
            if (socketRef.current === socket) socketRef.current = null;
            reconnectTimer = window.setTimeout(() => void connect(), 1600);
          });
          socket.addEventListener("error", () => socket.close());
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
    applySnapshot,
    config,
    handleLiveEvent,
    loadRun,
    loadSnapshot,
    namespace,
    subscribeSocket,
  ]);

  const unlockOperator = useCallback(
    async (token: string) => {
      try {
        await requestJson(config, "/operator/session", {
          method: "POST",
          body: JSON.stringify({ token }),
        });
        setOperator(true);
        announce("Operator controls unlocked for this session.");
        return true;
      } catch (error) {
        announce(`Operator unlock failed: ${(error as Error).message}`, "error");
        return false;
      }
    },
    [announce, config],
  );

  const lockOperator = useCallback(async () => {
    await requestJson(config, "/operator/session", { method: "DELETE" }).catch(() => undefined);
    setOperator(false);
    announce("Returned to the public read-only replay.");
  }, [announce, config]);

  const resetDemo = useCallback(async () => {
    setBusy("reset");
    try {
      const payload = await requestJson<ResetResponse>(config, "/demo/poison-rewind/reset", {
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
  }, [announce, config, loadIncidents, loadScenario, subscribeSocket, updateNamespace]);

  const poisonDemo = useCallback(async () => {
    if (!rewindAnchor) {
      announce("Reset the replay before importing stale guidance.", "error");
      return;
    }
    setBusy("poison");
    try {
      await requestJson(config, "/demo/poison-rewind/poison", {
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
  }, [announce, config, loadScenario, rewindAnchor]);

  const startRun = useCallback(async () => {
    if (!incident) {
      announce("Choose or reset a signature incident first.", "error");
      return;
    }
    setBusy("run");
    try {
      const result = await requestJson<RunStartResponse>(
        config,
        `/incidents/${encodeURIComponent(incident.slug)}/runs`,
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
  }, [announce, config, incident, incidentInput, loadRun]);

  const decideRun = useCallback(
    async (approved: boolean) => {
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
        await requestJson(config, `/runs/${encodeURIComponent(current.id)}/approval`, {
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
    [announce, config, loadRun],
  );

  const invalidatePreview = useCallback(() => setRewindPreview(null), []);

  const previewRewind = useCallback(async () => {
    const target = rewindAnchor || localInputToIso(rewindTimestamp);
    if (!target) {
      announce("Choose a valid rewind timestamp.", "error");
      return;
    }
    setBusy("preview");
    try {
      const preview = await requestJson<RewindPreview>(
        config,
        `/namespaces/${encodeURIComponent(namespaceRef.current)}/rewinds/preview`,
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
  }, [announce, config, rewindAnchor, rewindReason, rewindTimestamp]);

  const waitForOperation = useCallback(
    async (operationId: string) => {
      const pollSeconds = Math.max(60, Number(config.operationPollSeconds || 600));
      const deadline = Date.now() + pollSeconds * 1000;
      let lastOperation: MemoryOperation | null = null;
      while (Date.now() < deadline) {
        const operation = await requestJson<MemoryOperation>(
          config,
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
    [applySnapshot, config],
  );

  const executeRewind = useCallback(async () => {
    if (!rewindPreview) return;
    setBusy("execute");
    try {
      const accepted = await requestJson<RewindAccepted>(
        config,
        `/namespaces/${encodeURIComponent(namespaceRef.current)}/rewinds`,
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
    config,
    loadSnapshot,
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

  return {
    config,
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
    operator,
    notice,
    incidentInput,
    rewindAnchor,
    rewindTimestamp,
    rewindReason,
    rewindPreview,
    busy,
    retryInitialLoad,
    unlockOperator,
    lockOperator,
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
