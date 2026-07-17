const config = window.HINDSIGHT_CONFIG || {};
const params = new URLSearchParams(window.location.search);
const state = {
  namespace: params.get("namespace") || config.defaultNamespace || "demo:payments-poison-rewind",
  incidents: [],
  incident: null,
  run: null,
  operator: false,
  memories: new Map(),
  operations: new Map(),
  timeline: [],
  asOf: null,
  influence: [],
  memoryDetail: null,
  rewindPreview: null,
  rewindAnchor: null,
  socket: null,
  snapshotRequest: 0,
  incidentRequest: 0,
  runRequest: 0,
  influenceRequest: 0,
  activeRunId: null,
  runPoll: null
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const elements = {
  connection: $("#connection"),
  operatorButton: $("#operatorButton"),
  operatorLabel: $("#operatorLabel"),
  operatorPanel: $("#operatorPanel"),
  operatorForm: $("#operatorForm"),
  operatorToken: $("#operatorToken"),
  lockButton: $("#lockButton"),
  incidentHeading: $("#incidentHeading"),
  incidentSummary: $("#incidentSummary"),
  incidentSeverity: $("#incidentSeverity"),
  incidentService: $("#incidentService"),
  namespace: $("#namespace"),
  runStatus: $("#runStatus"),
  incidentSelect: $("#incidentSelect"),
  incidentInput: $("#incidentInput"),
  incidentForm: $("#incidentForm"),
  phaseRail: $("#phaseRail"),
  approvalActions: $("#approvalActions"),
  timeline: $("#timeline"),
  timeLabel: $("#timeLabel"),
  memories: $("#memories"),
  memoryCount: $("#memoryCount"),
  beliefTitle: $("#beliefTitle"),
  influenceCount: $("#influenceCount"),
  influenceList: $("#influenceList"),
  planText: $("#planText"),
  proposedAction: $("#proposedAction"),
  operations: $("#operations"),
  operationCount: $("#operationCount"),
  rewindTimestamp: $("#rewindTimestamp"),
  rewindReason: $("#rewindReason"),
  rewindPreview: $("#rewindPreview"),
  executeRewind: $("#executeRewind"),
  notice: $("#notice")
};

function apiUrl(path) {
  return `${config.apiBase || "/v1"}${path}`;
}

async function request(path, options = {}) {
  const response = await fetch(apiUrl(path), {
    credentials: "include",
    ...options,
    headers: {"content-type": "application/json", ...(options.headers || {})}
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = body.detail || body.error || detail;
    } catch (_) {
      // The status text is enough when a proxy returns a non-JSON error.
    }
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}

function setConnection(label, kind = "") {
  elements.connection.className = `connection ${kind}`.trim();
  elements.connection.querySelector("span").textContent = label;
}

function notify(message, kind = "") {
  elements.notice.textContent = message;
  elements.notice.className = `notice ${kind}`.trim();
  elements.notice.hidden = false;
  clearTimeout(notify.timeout);
  notify.timeout = setTimeout(() => { elements.notice.hidden = true; }, 5000);
}

function updateOperatorState() {
  elements.operatorLabel.textContent = state.operator ? "Operator" : "Read-only";
  elements.lockButton.hidden = !state.operator;
  $$('[data-operator]').forEach((element) => {
    element.disabled = !state.operator;
    element.setAttribute("aria-disabled", String(!state.operator));
  });
  elements.executeRewind.disabled = !state.operator || !state.rewindPreview;
}

async function establishOperatorSession() {
  const hash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const fragmentToken = hash.get("operator");
  if (fragmentToken) {
    try {
      await request("/operator/session", {
        method: "POST",
        body: JSON.stringify({token: fragmentToken})
      });
      history.replaceState(null, "", `${location.pathname}${location.search}`);
    } catch (error) {
      notify(`Operator unlock failed: ${error.message}`, "error");
    }
  }
  try {
    state.operator = Boolean((await request("/operator/session")).operator);
  } catch (_) {
    state.operator = false;
  }
  updateOperatorState();
}

async function loadIncidents(preferredSlug = null) {
  try {
    const payload = await request("/incidents");
    state.incidents = payload.items || [];
    elements.incidentSelect.innerHTML = state.incidents.length
      ? state.incidents.map((incident) => `<option value="${escapeHtml(incident.slug)}">${escapeHtml(incident.title)}</option>`).join("")
      : '<option value="">No incidents yet</option>';
    const selected = state.incidents.find((item) => item.slug === preferredSlug)
      || state.incidents.find((item) => (
        item.slug.startsWith("demo-payments-checkout-latency:")
        && item.latest_run_status === "completed"
      ))
      || state.incidents.find((item) => item.slug === "demo-payments-checkout-latency")
      || state.incidents[0];
    if (selected) {
      elements.incidentSelect.value = selected.slug;
      await selectIncident(selected.slug);
    } else {
      renderIncident();
    }
  } catch (error) {
    // The original local SSE server has no product API; its memory view remains useful.
    elements.incidentSelect.innerHTML = '<option value="">Product API unavailable</option>';
    renderIncident();
  }
}

async function selectIncident(slug) {
  if (!slug) return;
  const requestId = ++state.incidentRequest;
  clearTimeout(state.runPoll);
  state.activeRunId = null;
  try {
    const incident = await request(`/incidents/${encodeURIComponent(slug)}`);
    if (requestId !== state.incidentRequest) return;
    const latestRun = incident.runs?.[0];
    const run = latestRun ? await request(`/runs/${latestRun.id}`) : null;
    if (requestId !== state.incidentRequest) return;
    state.incident = incident;
    state.run = run;
    state.activeRunId = run?.id || null;
    if (run?.namespace) {
      state.namespace = run.namespace;
      const url = new URL(window.location.href);
      url.searchParams.set("namespace", state.namespace);
      history.replaceState({}, "", url);
    }
    elements.incidentInput.value = run?.user_input || incident.summary || "";
    renderIncident();
    renderRun();
    subscribeSocket();
    await loadSnapshot();
    if (run?.decision_id) await loadInfluence(run.decision_id);
    else clearInfluence();
  } catch (error) {
    if (requestId !== state.incidentRequest) return;
    notify(`Incident could not be loaded: ${error.message}`, "error");
  }
}

function renderIncident() {
  const incident = state.incident;
  elements.incidentHeading.textContent = incident?.title || "Memory Dashboard";
  elements.incidentSummary.textContent = incident?.summary || "Watch a decision change when its memory changes.";
  elements.incidentSeverity.textContent = incident?.severity || "—";
  elements.incidentService.textContent = incident?.service_slug || "—";
  elements.namespace.textContent = state.namespace;
  elements.runStatus.textContent = state.run?.status?.replaceAll("_", " ") || "No run";
}

function renderRun() {
  const run = state.run;
  renderIncident();
  elements.planText.textContent = run?.plan || "Select a completed run to trace its reasoning back to memory.";
  elements.proposedAction.textContent = run?.proposed_action || "";
  elements.approvalActions.hidden = run?.status !== "awaiting_approval";

  const phases = ["triage", "recall", "plan", "approval", "reflection"];
  const seen = new Set((run?.events || []).map((event) => event.phase));
  const currentPhase = (run?.events || []).at(-1)?.phase;
  phases.forEach((phase) => {
    const item = elements.phaseRail.querySelector(`[data-phase="${phase}"]`);
    item.classList.toggle("complete", seen.has(phase) && phase !== currentPhase);
    item.classList.toggle("active", phase === currentPhase && !["completed", "rejected", "failed"].includes(run?.status));
  });
  if (["completed", "rejected"].includes(run?.status)) {
    elements.phaseRail.querySelectorAll("li").forEach((item) => item.classList.add("complete"));
  }
}

async function loadRun(runId, {poll = false} = {}) {
  if (state.activeRunId && runId !== state.activeRunId) return;
  const requestId = ++state.runRequest;
  try {
    const run = await request(`/runs/${encodeURIComponent(runId)}`);
    if (requestId !== state.runRequest || runId !== state.activeRunId) return;
    state.run = run;
    subscribeSocket();
    renderRun();
    if (run.decision_id) await loadInfluence(run.decision_id);
    else clearInfluence();
    if (poll && !["completed", "rejected", "failed", "awaiting_approval"].includes(run.status)) {
      clearTimeout(state.runPoll);
      state.runPoll = setTimeout(() => loadRun(runId, {poll: true}), 1400);
    }
  } catch (error) {
    if (requestId !== state.runRequest) return;
    notify(`Run status could not be loaded: ${error.message}`, "error");
  }
}

async function loadInfluence(decisionId) {
  const requestId = ++state.influenceRequest;
  try {
    const payload = await request(`/decisions/${encodeURIComponent(decisionId)}/influence`);
    if (requestId !== state.influenceRequest || decisionId !== state.run?.decision_id) return;
    state.influence = payload.memories || [];
    state.memoryDetail = null;
    renderInfluence();
  } catch (error) {
    if (requestId !== state.influenceRequest) return;
    state.influence = [];
    renderInfluence();
    notify(`Decision influence could not be loaded: ${error.message}`, "error");
  }
}

function clearInfluence() {
  state.influenceRequest += 1;
  state.influence = [];
  state.memoryDetail = null;
  renderInfluence();
}

function renderInfluence() {
  elements.influenceCount.textContent = `${state.influence.length} read${state.influence.length === 1 ? "" : "s"}`;
  const detail = state.memoryDetail ? renderMemoryDetail(state.memoryDetail) : "";
  elements.influenceList.innerHTML = detail + state.influence.map((item) => {
    const memory = item.memory || {};
    const provenance = item.provenance || {};
    return `<details class="influence-memory ${item.status === "invalidated" ? "invalidated" : ""}">
      <summary><span>${escapeHtml(memory.content || "Memory unavailable")}</span><span class="status">${escapeHtml(item.status)}</span></summary>
      <div class="provenance">
        <div>read ${escapeHtml(formatTime(item.read?.read_at))} by ${escapeHtml(item.read?.reader)}</div>
        <div>origin ${escapeHtml(provenance.writer || memory.writer)} · ${escapeHtml(provenance.source_ref || memory.source_ref)}</div>
        <div>${escapeHtml(provenance.justification || memory.justification)}</div>
        ${provenance.invalidation_reason ? `<div>invalidated: ${escapeHtml(provenance.invalidation_reason)}</div>` : ""}
      </div>
    </details>`;
  }).join("");
}

function renderMemoryDetail(payload) {
  const memory = payload.memory || {};
  const provenance = payload.provenance || {};
  return `<article class="provenance" aria-label="Selected memory provenance">
    <span class="eyebrow">Selected memory</span>
    <strong>${escapeHtml(memory.content)}</strong>
    <div>writer ${escapeHtml(provenance.writer || memory.writer)} · source ${escapeHtml(provenance.source_ref || memory.source_ref)}</div>
    <div>valid ${escapeHtml(formatTime(memory.t_valid))}${memory.t_invalid ? ` → ${escapeHtml(formatTime(memory.t_invalid))}` : " → now"}</div>
    ${memory.invalidation_reason ? `<div>invalidated: ${escapeHtml(memory.invalidation_reason)}</div>` : ""}
  </article>`;
}

function snapshotUrl(asOf = null) {
  if (config.snapshotBase) {
    const url = new URL(config.snapshotBase, window.location.origin);
    url.searchParams.set("namespace", state.namespace);
    if (asOf) url.searchParams.set("as_of", asOf);
    return url.toString();
  }
  const url = new URL(apiUrl(`/namespaces/${encodeURIComponent(state.namespace)}/beliefs`), window.location.origin);
  if (asOf) url.searchParams.set("as_of", asOf);
  return url.toString();
}

async function loadSnapshot(asOf = null) {
  const requestId = ++state.snapshotRequest;
  try {
    const response = await fetch(snapshotUrl(asOf), {credentials: "include"});
    if (!response.ok) throw new Error(await response.text());
    const snapshot = await response.json();
    if (requestId !== state.snapshotRequest) return;
    applySnapshot(snapshot);
    setConnection(asOf ? "Historical" : "Live", asOf ? "" : "live");
  } catch (error) {
    setConnection("Disconnected", "error");
    notify(`Belief state could not be loaded: ${error.message}`, "error");
  }
}

function applySnapshot(snapshot) {
  state.memories = new Map((snapshot.memories || []).map((memory) => [memory.id, memory]));
  state.operations = new Map((snapshot.operations || []).map((operation) => [operation.id, operation]));
  state.timeline = snapshot.timeline || [];
  state.asOf = snapshot.as_of || null;
  renderBeliefs();
}

function mergeTimeline(values) {
  const merged = new Set(state.timeline);
  values.filter(Boolean).forEach((value) => merged.add(value));
  state.timeline = [...merged].sort();
}

function renderBeliefs() {
  const memories = [...state.memories.values()];
  const current = memories.filter((memory) => memory.status !== "invalidated");
  const invalid = memories.length - current.length;
  elements.beliefTitle.textContent = state.asOf ? "Beliefs As Of" : "Current Beliefs";
  elements.memoryCount.textContent = `${current.length} live · ${invalid} invalid`;
  elements.operationCount.textContent = `${state.operations.size} operation${state.operations.size === 1 ? "" : "s"}`;
  elements.memories.innerHTML = memories.length
    ? memories.map(renderMemory).join("")
    : `<div class="empty-state"><span class="eyebrow">No beliefs yet</span><h3>Start with known-good context</h3><p>Unlock operator controls and reset the demo to seed the payment-latency memory.</p></div>`;
  elements.operations.innerHTML = [...state.operations.values()].reverse().map(renderOperation).join("");
  elements.timeline.max = Math.max(state.timeline.length - 1, 0);
  elements.timeline.disabled = state.timeline.length === 0;
  if (!state.asOf) {
    elements.timeline.value = state.timeline.length ? state.timeline.length - 1 : 0;
    elements.timeLabel.textContent = "Live belief state";
  } else {
    elements.timeline.value = Math.max(state.timeline.indexOf(state.asOf), 0);
    elements.timeLabel.textContent = `As of ${formatTime(state.asOf)}`;
  }
}

function renderMemory(memory) {
  const invalid = memory.status === "invalidated";
  const review = memory.trust_status === "review_required";
  return `<button class="memory ${invalid ? "invalidated" : ""}" type="button" data-memory-id="${escapeHtml(memory.id)}">
    <span class="memory-bar" aria-hidden="true"></span>
    <span>
      <span class="memory-content">${escapeHtml(memory.content)}</span>
      <span class="memory-meta">
        <span class="memory-status">${invalid ? "invalidated" : review ? "review required" : "current"}</span>
        <span>${escapeHtml(memory.writer)}</span>
        <span>${escapeHtml(formatTime(memory.written_at || memory.t_valid))}</span>
        ${memory.invalidation_reason ? `<span>${escapeHtml(memory.invalidation_reason)}</span>` : ""}
      </span>
    </span>
    <span class="memory-arrow" aria-hidden="true">↗</span>
  </button>`;
}

function renderOperation(operation) {
  const effects = operation.effects || [];
  const reviewCount = effects.filter((effect) => effect.effect_type === "review_required").length;
  const operationType = operation.operation_type || "operation";
  const status = operation.status || "completed";
  return `<article class="operation operation-${escapeHtml(status)}" data-operation-id="${escapeHtml(operation.id || "")}" data-operation-type="${escapeHtml(operationType)}" data-operation-status="${escapeHtml(status)}"><strong>${escapeHtml(operationType)} · ${escapeHtml(status)}</strong><span>${escapeHtml(operation.reason)} · closed ${(operation.invalidated_memory_ids || []).length} · created ${(operation.restored_memory_ids || []).length}${reviewCount ? ` · review ${reviewCount}` : ""} · ${escapeHtml(formatTime(operation.created_at))}</span>${operation.failure_detail ? `<span>${escapeHtml(operation.failure_detail)}</span>` : ""}</article>`;
}

async function showMemory(memoryId) {
  try {
    state.memoryDetail = await request(`/memories/semantic/${encodeURIComponent(memoryId)}`);
    renderInfluence();
    $("#influenceTitle").scrollIntoView({behavior: "smooth", block: "start"});
  } catch (error) {
    notify(`Memory provenance could not be loaded: ${error.message}`, "error");
  }
}

async function startRun() {
  if (!state.incident) return notify("Choose or create an incident first.", "error");
  setBusy($("#startRun"), true, "Queuing…");
  try {
    const result = await request(`/incidents/${encodeURIComponent(state.incident.slug)}/runs`, {
      method: "POST",
      headers: {"Idempotency-Key": crypto.randomUUID()},
      body: JSON.stringify({
        namespace: state.namespace,
        user_input: elements.incidentInput.value.trim()
      })
    });
    state.activeRunId = result.run_id;
    notify("Agent run queued. Phase events will appear here.");
    await loadRun(result.run_id, {poll: true});
  } catch (error) {
    notify(`Run could not start: ${error.message}`, "error");
  } finally {
    setBusy($("#startRun"), false, "Analyze incident");
  }
}

async function decideRun(approved) {
  if (!state.run) return;
  try {
    await request(`/runs/${encodeURIComponent(state.run.id)}/approval`, {
      method: "POST",
      body: JSON.stringify({approved})
    });
    notify(approved ? "Recommendation approved for reflection." : "Recommendation rejected and retained in the audit trail.");
    await loadRun(state.run.id, {poll: true});
  } catch (error) {
    notify(`Decision could not be recorded: ${error.message}`, "error");
  }
}

async function resetDemo() {
  setBusy($("#resetDemo"), true, "Resetting…");
  try {
    const payload = await request("/demo/poison-rewind/reset", {
      method: "POST",
      body: JSON.stringify({namespace: state.namespace})
    });
    state.namespace = payload.namespace;
    subscribeSocket();
    const url = new URL(window.location.href);
    url.searchParams.set("namespace", state.namespace);
    history.replaceState({}, "", url);
    state.rewindAnchor = payload.rewind_anchor;
    elements.rewindTimestamp.value = isoToLocalInput(state.rewindAnchor);
    invalidateRewindPreview();
    await loadIncidents(payload.incident?.slug);
    notify("Known-good payment memory restored. The demo is ready.");
  } catch (error) {
    notify(`Demo reset failed: ${error.message}`, "error");
  } finally {
    setBusy($("#resetDemo"), false, "Reset");
  }
}

async function poisonDemo() {
  if (!state.rewindAnchor) {
    return notify("Reset the demo before inserting poisoned memory.", "error");
  }
  setBusy($("#poisonDemo"), true, "Injecting…");
  try {
    await request("/demo/poison-rewind/poison", {
      method: "POST",
      body: JSON.stringify({namespace: state.namespace})
    });
    await loadSnapshot();
    notify("Poisoned certificate memory inserted with provenance.");
  } catch (error) {
    notify(`Poison injection failed: ${error.message}`, "error");
  } finally {
    setBusy($("#poisonDemo"), false, "Inject poison");
  }
}

async function previewRewind() {
  const target = state.rewindAnchor || localInputToIso(elements.rewindTimestamp.value);
  if (!target) return notify("Choose a valid rewind timestamp.", "error");
  try {
    const preview = await request(`/namespaces/${encodeURIComponent(state.namespace)}/rewinds/preview`, {
      method: "POST",
      body: JSON.stringify({
        target_timestamp: target,
        reason: elements.rewindReason.value.trim() || "Operator-requested rewind"
      })
    });
    state.rewindPreview = preview;
    const effects = preview.effect_payload || {};
    elements.rewindPreview.innerHTML = `<strong>${(effects.close_memory_ids || []).length} versions will close.</strong><br>${(effects.reassertions || []).length} historical beliefs will be reasserted as new audited versions. Preview expires ${escapeHtml(formatTime(preview.expires_at))}.`;
    updateOperatorState();
  } catch (error) {
    state.rewindPreview = null;
    updateOperatorState();
    notify(`Rewind preview failed: ${error.message}`, "error");
  }
}

async function executeRewind() {
  if (!state.rewindPreview) return;
  setBusy(elements.executeRewind, true, "Rewinding…");
  try {
    const accepted = await request(`/namespaces/${encodeURIComponent(state.namespace)}/rewinds`, {
      method: "POST",
      headers: {"Idempotency-Key": crypto.randomUUID()},
      body: JSON.stringify({
        preview_id: state.rewindPreview.id,
        fingerprint: state.rewindPreview.fingerprint
      })
    });
    state.operations.set(accepted.operation_id, {
      id: accepted.operation_id,
      operation_type: "rewind",
      status: accepted.status || "queued",
      reason: elements.rewindReason.value.trim() || "Operator-requested rewind",
      invalidated_memory_ids: [],
      restored_memory_ids: [],
      created_at: new Date().toISOString()
    });
    renderBeliefs();
    state.rewindPreview = null;
    notify("Rewind queued. The approved preview will be verified before any memory changes.");
    const operation = await waitForOperation(accepted.operation_id);
    await loadSnapshot();
    if (operation.status === "completed") notify("Belief state rewound. Historical versions remain visible for audit.");
    else notify(`Rewind ended in ${operation.status}: ${operation.failure_detail || "state changed"}`, "error");
  } catch (error) {
    notify(`Rewind failed: ${error.message}`, "error");
  } finally {
    setBusy(elements.executeRewind, false, "Execute rewind");
    updateOperatorState();
  }
}

async function waitForOperation(operationId) {
  const pollSeconds = Math.max(60, Number(config.operationPollSeconds || 600));
  const deadline = Date.now() + pollSeconds * 1000;
  let lastOperation = null;
  while (Date.now() < deadline) {
    const operation = await request(`/memory/operations/${encodeURIComponent(operationId)}`);
    lastOperation = operation;
    state.operations.set(operation.id, operation);
    renderBeliefs();
    if (["completed", "conflict", "failed"].includes(operation.status)) return operation;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  const detail = lastOperation?.failure_detail ? `: ${lastOperation.failure_detail}` : "";
  throw new Error(`operation did not reach a terminal state; last status ${lastOperation?.status || "unknown"}${detail}`);
}

function connectEvents() {
  if (config.websocketUrl) return connectWebSocket();
  if (config.eventsBase) return connectSse();
  const interval = Math.max(1500, Number(config.pollIntervalMs || 4000));
  setInterval(() => {
    if (!state.asOf) loadSnapshot();
    if (state.run && !["completed", "rejected", "failed"].includes(state.run.status)) loadRun(state.run.id);
  }, interval);
}

function connectSse() {
  const url = `${config.eventsBase}?namespace=${encodeURIComponent(state.namespace)}`;
  const events = new EventSource(url);
  events.addEventListener("open", () => setConnection("Live", "live"));
  events.addEventListener("snapshot", (event) => applySnapshot(JSON.parse(event.data)));
  events.addEventListener("memory", (event) => handleLiveEvent(JSON.parse(event.data)));
  events.addEventListener("operation", (event) => handleLiveEvent(JSON.parse(event.data)));
  events.addEventListener("error", () => setConnection("Reconnecting", "error"));
}

function connectWebSocket() {
  const socket = new WebSocket(config.websocketUrl);
  state.socket = socket;
  socket.addEventListener("open", () => {
    setConnection("Live", "live");
    subscribeSocket();
  });
  socket.addEventListener("message", (event) => handleLiveEvent(JSON.parse(event.data)));
  socket.addEventListener("close", () => {
    setConnection("Reconnecting", "error");
    if (state.socket === socket) state.socket = null;
    setTimeout(connectWebSocket, 1600);
  });
  socket.addEventListener("error", () => socket.close());
}

function subscribeSocket() {
  if (state.socket?.readyState !== WebSocket.OPEN) return;
  state.socket.send(JSON.stringify({
    type: "subscribe",
    namespace: state.namespace,
    run_id: state.run?.id || null
  }));
}

function handleLiveEvent(payload) {
  if (payload.namespace && payload.namespace !== state.namespace) return;
  const type = payload.type || payload.event;
  const data = payload.data || payload;
  if (type === "memory" && data.memory) {
    state.memories.set(data.memory.id, data.memory);
    mergeTimeline([data.memory.t_valid, data.memory.written_at, data.memory.t_invalid, data.memory.invalidated_at]);
    if (!state.asOf) renderBeliefs();
  }
  if (type === "operation" && data.operation) {
    state.operations.set(data.operation.id, data.operation);
    mergeTimeline([data.operation.target_timestamp, data.operation.created_at]);
    if (!state.asOf) renderBeliefs();
  }
  if (["run", "run_event"].includes(type) && (payload.run_id || data.run_id)) {
    loadRun(payload.run_id || data.run_id);
  }
}

function setBusy(button, busy, label) {
  button.disabled = busy || (!state.operator && button.hasAttribute("data-operator"));
  button.textContent = label;
  button.setAttribute("aria-busy", String(busy));
}

function invalidateRewindPreview() {
  state.rewindPreview = null;
  elements.rewindPreview.textContent = "Choose a point on the belief timeline to preview its impact.";
  updateOperatorState();
}

function formatTime(value) {
  if (!value) return "unknown";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString([], {dateStyle: "medium", timeStyle: "medium"});
}

function isoToLocalInput(value) {
  const date = new Date(value);
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 19);
}

function localInputToIso(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[character]);
}

elements.operatorButton.addEventListener("click", () => {
  elements.operatorPanel.hidden = !elements.operatorPanel.hidden;
  elements.operatorButton.setAttribute("aria-expanded", String(!elements.operatorPanel.hidden));
  if (!elements.operatorPanel.hidden) elements.operatorToken.focus();
});
elements.operatorForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await request("/operator/session", {method: "POST", body: JSON.stringify({token: elements.operatorToken.value})});
    elements.operatorToken.value = "";
    state.operator = true;
    updateOperatorState();
    elements.operatorPanel.hidden = true;
    notify("Operator controls unlocked for this session.");
  } catch (error) {
    notify(`Operator unlock failed: ${error.message}`, "error");
  }
});
elements.lockButton.addEventListener("click", async () => {
  await request("/operator/session", {method: "DELETE"});
  state.operator = false;
  updateOperatorState();
});
elements.incidentSelect.addEventListener("change", () => selectIncident(elements.incidentSelect.value));
elements.incidentForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(elements.incidentForm);
  try {
    const incident = await request("/incidents", {method: "POST", body: JSON.stringify(Object.fromEntries(form))});
    elements.incidentForm.reset();
    await loadIncidents(incident.slug);
    notify("Incident created.");
  } catch (error) {
    notify(`Incident could not be created: ${error.message}`, "error");
  }
});
$("#startRun").addEventListener("click", startRun);
$("#approveRun").addEventListener("click", () => decideRun(true));
$("#rejectRun").addEventListener("click", () => decideRun(false));
$("#resetDemo").addEventListener("click", resetDemo);
$("#poisonDemo").addEventListener("click", poisonDemo);
$("#previewRewind").addEventListener("click", previewRewind);
elements.executeRewind.addEventListener("click", executeRewind);
elements.rewindTimestamp.addEventListener("input", () => {
  state.rewindAnchor = null;
  invalidateRewindPreview();
});
elements.rewindReason.addEventListener("input", invalidateRewindPreview);
elements.memories.addEventListener("click", (event) => {
  const memory = event.target.closest("[data-memory-id]");
  if (memory) showMemory(memory.dataset.memoryId);
});
elements.timeline.addEventListener("input", () => {
  const selected = state.timeline[Number(elements.timeline.value)];
  if (selected) loadSnapshot(selected);
});
$("#liveButton").addEventListener("click", () => loadSnapshot());

await establishOperatorSession();
renderIncident();
renderRun();
await loadIncidents();
if (!state.incident) await loadSnapshot(params.get("as_of"));
connectEvents();
