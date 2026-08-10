import { ListChecks, SignOut, Warning } from "@phosphor-icons/react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { formatTime } from "@/lib/format";
import type { Incident, RewindPreview, Run, SignatureScenario, Snapshot } from "@/types";

const phases = [
  { key: "triage", label: "triage" },
  { key: "recall", label: "recall" },
  { key: "plan", label: "cited proposal" },
  { key: "approval", label: "approval" },
  { key: "action", label: "recommendation" },
  { key: "observation", label: "diagnostics" },
  { key: "reflection", label: "reflection" },
];

const walkthroughSteps = [
  {
    id: "reset",
    label: "Reset the replay",
    detail: "Restore the known-good baseline and create a fresh signature namespace.",
  },
  {
    id: "compromise",
    label: "Import stale guidance",
    detail: "Import traced retry-amplifying guidance into normal governed memory history.",
  },
  {
    id: "analyze",
    label: "Analyze the incident",
    detail: "Run the agent and inspect which memories shaped the recommendation.",
  },
  {
    id: "review",
    label: "Review recommendation",
    detail: "Approve or reject the recommendation bound to its cited memory selection.",
  },
  {
    id: "preview",
    label: "Preview the rewind",
    detail: "Review which belief versions will close before any mutation is queued.",
  },
  {
    id: "execute",
    label: "Execute the rewind",
    detail: "Execute the approved preview and wait for the audited operation to complete.",
  },
  {
    id: "reanalyze",
    label: "Re-analyze and approve",
    detail: "Analyze again, confirm the stale guidance is absent, then approve the corrected plan.",
  },
  {
    id: "history",
    label: "Inspect history",
    detail: "Use the belief-state timeline to inspect prior versions, then return to live state.",
  },
] as const;

type WalkthroughStep = (typeof walkthroughSteps)[number]["id"];

export function deriveWalkthroughStep({
  rewindAnchor,
  scenario,
  snapshot,
  run,
  rewindPreview,
}: {
  rewindAnchor: string | null;
  scenario: SignatureScenario | null;
  snapshot: Snapshot | null;
  run: Run | null;
  rewindPreview: RewindPreview | null;
}): WalkthroughStep {
  if (!rewindAnchor) return "reset";
  const completedRewind = Boolean(
    snapshot?.operations.some(
      (operation) => operation.operation_type === "rewind" && operation.status === "completed",
    ),
  );
  const compromisedMemoryId = scenario?.stages.compromised_memory_id;
  const activeCompromisedGuidance = Boolean(
    compromisedMemoryId &&
      snapshot?.memories.some(
        (memory) =>
          memory.id === compromisedMemoryId &&
          memory.status !== "invalidated" &&
          !memory.t_invalid,
      ),
  );
  if (!activeCompromisedGuidance && !completedRewind) return "compromise";
  if (!completedRewind) {
    if (rewindPreview) return "execute";
    if (run?.status === "awaiting_approval") return "review";
    if (run?.status === "rejected") return "preview";
    return "analyze";
  }
  if (run?.status === "awaiting_approval") return "review";
  if (scenario?.status === "completed" && (!run || run.status === "completed")) return "history";
  return "reanalyze";
}

export function OperatorConsole({
  incidents,
  incident,
  run,
  incidentInput,
  busy,
  rewindAnchor,
  scenario,
  snapshot,
  rewindTimestamp,
  rewindReason,
  rewindPreview,
  onIncident,
  onIncidentInput,
  onReset,
  onPoison,
  onRun,
  onDecision,
  onRewindTimestamp,
  onRewindReason,
  onPreview,
  onExecute,
  onSignOut,
}: {
  incidents: Incident[];
  incident: Incident | null;
  run: Run | null;
  incidentInput: string;
  busy: string | null;
  rewindAnchor: string | null;
  scenario: SignatureScenario | null;
  snapshot: Snapshot | null;
  rewindTimestamp: string;
  rewindReason: string;
  rewindPreview: RewindPreview | null;
  onIncident: (slug: string) => void;
  onIncidentInput: (value: string) => void;
  onReset: () => void;
  onPoison: () => void;
  onRun: () => void;
  onDecision: (approved: boolean) => void;
  onRewindTimestamp: (value: string) => void;
  onRewindReason: (value: string) => void;
  onPreview: () => void;
  onExecute: () => void;
  onSignOut: () => void;
}) {
  const [walkthroughOpen, setWalkthroughOpen] = useState(false);
  const eventTraceAvailable = Array.isArray(run?.events);
  const eventPhases = new Set((run?.events || []).map((event) => event.phase));
  const currentPhase = run?.events?.at(-1)?.phase;
  const terminalRun = Boolean(run?.status.match(/completed|rejected|failed|cancelled/));
  const walkthroughStep = deriveWalkthroughStep({
    rewindAnchor,
    scenario,
    snapshot,
    run,
    rewindPreview,
  });
  const walkthroughIndex = walkthroughSteps.findIndex((step) => step.id === walkthroughStep);
  const recommendationId = run?.action_trace?.recommendation?.id;
  const remediationAction = run?.action_trace?.remediation_action;
  const remediationPreview = run?.action_trace?.preview;
  const remediation = run?.action_trace?.mode === "governed_memory_remediation";
  const selectionFingerprint = run?.action_trace?.selection?.fingerprint;
  const approvalIdentityReady = remediation
    ? Boolean(
        remediationAction?.id &&
          selectionFingerprint &&
          run?.action_trace?.observation_fingerprint &&
          remediationPreview?.id &&
          remediationPreview?.fingerprint,
      )
    : Boolean(recommendationId && selectionFingerprint);
  const describedBy = walkthroughOpen ? "walkthroughCurrent" : undefined;
  const currentControl = (step: WalkthroughStep) =>
    walkthroughOpen && walkthroughStep === step
      ? { "aria-describedby": describedBy, "data-walkthrough-current": "true" }
      : {};
  return (
    <section className="operator-console" aria-label="Protected operator controls">
      <header>
        <div>
          <p className="section-kicker">Protected surface</p>
          <h2>Operator console</h2>
        </div>
        <div className="operator-header-actions">
          <Button
            id="walkthroughToggle"
            type="button"
            variant="ghost"
            size="compact"
            aria-expanded={walkthroughOpen}
            aria-controls="operatorWalkthrough"
            onClick={() => setWalkthroughOpen((value) => !value)}
          >
            <ListChecks aria-hidden="true" size={14} />
            {walkthroughOpen ? "Hide walkthrough" : "Walkthrough"}
          </Button>
          <Button id="signOutButton" type="button" variant="ghost" size="compact" onClick={onSignOut}>
            <SignOut aria-hidden="true" size={14} />
            Sign out
          </Button>
        </div>
      </header>

      <aside id="operatorWalkthrough" className="operator-walkthrough" hidden={!walkthroughOpen}>
        <div className="walkthrough-summary">
          <p className="section-kicker">Guided replay</p>
          <h3>Follow the correction sequence</h3>
          <p id="walkthroughCurrent" role="status" aria-live="polite">
            {walkthroughSteps[walkthroughIndex].detail}
          </p>
          {walkthroughStep === "history" ? (
            <a id="walkthroughHistory" href="#timeline">Open belief history</a>
          ) : null}
        </div>
        <ol aria-label="Operator replay walkthrough">
          {walkthroughSteps.map((step, index) => (
            <li
              key={step.id}
              className={index < walkthroughIndex ? "complete" : index === walkthroughIndex ? "current" : ""}
              aria-current={index === walkthroughIndex ? "step" : undefined}
            >
              <span aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
              {step.label}
            </li>
          ))}
        </ol>
      </aside>

      <div className="operator-fields">
        <div className="field">
          <label htmlFor="incidentSelect">Incident</label>
          <select
            id="incidentSelect"
            value={incident?.slug || ""}
            onChange={(event) => onIncident(event.target.value)}
          >
            <option value="">{incidents.length ? "Choose an incident" : "No incidents yet"}</option>
            {incidents.map((item) => (
              <option key={item.slug} value={item.slug}>
                {item.title}
              </option>
            ))}
          </select>
        </div>
        <div className="field report-field">
          <label htmlFor="incidentInput">Current report</label>
          <textarea
            id="incidentInput"
            rows={2}
            value={incidentInput}
            onChange={(event) => onIncidentInput(event.target.value)}
          />
        </div>
        <div className="operator-actions">
          <Button
            id="resetDemo"
            type="button"
            data-operator
            disabled={busy === "reset"}
            onClick={onReset}
            {...currentControl("reset")}
          >
            {busy === "reset" ? "Resetting" : "Reset"}
          </Button>
          <Button
            id="poisonDemo"
            type="button"
            variant="danger"
            data-operator
            disabled={busy === "poison"}
            onClick={onPoison}
            {...currentControl("compromise")}
          >
            {busy === "poison" ? "Importing" : "Import stale guidance"}
          </Button>
          <Button
            id="startRun"
            type="button"
            variant="primary"
            data-operator
            disabled={busy === "run"}
            onClick={onRun}
            {...currentControl(walkthroughStep === "reanalyze" ? "reanalyze" : "analyze")}
          >
            {busy === "run" ? "Queuing" : "Analyze incident"}
          </Button>
        </div>
      </div>

      {!eventTraceAvailable ? (
        <p className="phase-trace-unavailable">Phase trace unavailable</p>
      ) : null}
      <ol id="phaseRail" className="phase-rail" aria-label="Agent run phases">
        {phases.map((phase) => {
          const observed = eventPhases.has(phase.key);
          const failed = run?.status === "failed" && phase.key === currentPhase;
          const complete = observed && !failed && (terminalRun || phase.key !== currentPhase);
          const active = phase.key === currentPhase && !terminalRun;
          const state = !eventTraceAvailable
            ? "unavailable"
            : failed
              ? "failed"
              : complete
                ? "complete"
                : active
                  ? "active"
                  : terminalRun
                    ? "not-observed"
                    : "pending";
          return (
            <li
              key={phase.key}
              data-phase={phase.key}
              data-phase-state={state}
              className={
                ["pending", "unavailable", "not-observed"].includes(state) ? "" : state
              }
              aria-current={active ? "step" : undefined}
              title={
                state === "unavailable"
                  ? "Phase state unavailable"
                  : state === "not-observed"
                    ? "Phase not observed"
                    : undefined
              }
            >
              <span aria-hidden="true" />
              {phase.key === "action" && remediation ? "governed retraction" : phase.label}
              <span className="sr-only">
                {`, ${state}`}
              </span>
            </li>
          );
        })}
      </ol>

      <div id="approvalActions" className="approval-actions" hidden={run?.status !== "awaiting_approval"}>
        <div>
          <strong>Decision awaits operator review</strong>
          <span>
            {approvalIdentityReady
              ? remediation
                ? remediationAction?.target_excerpt || "Governed-memory target verified"
                : run?.action_trace?.recommendation?.summary || "Recommendation identity verified"
              : "Approval identity unavailable. Refresh or rerun the analysis."}
          </span>
          {remediation && approvalIdentityReady ? (
            <>
              <span>
                {`${remediationPreview?.effect_count ?? 0} bounded mutation${remediationPreview?.effect_count === 1 ? "" : "s"} · expires ${formatTime(remediationPreview?.expires_at)} · fingerprint ${remediationPreview?.fingerprint?.slice(0, 12)}`}
              </span>
              <ul aria-label="Approval-bound retraction effects">
                {(remediationPreview?.effects?.close_memory_ids || []).map((memoryId) => (
                  <li key={`close:${memoryId}`}>{`Close memory ${memoryId}`}</li>
                ))}
                {(remediationPreview?.effects?.review_resolutions || []).map((resolution) => (
                  <li key={`review:${resolution.id || resolution.semantic_memory_id}`}>
                    {`Resolve review ${resolution.id || "unavailable"} for memory ${resolution.semantic_memory_id || "unavailable"} as ${resolution.status || "unavailable"}`}
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </div>
        <Button
          id="rejectRun"
          type="button"
          variant="danger"
          data-operator
          disabled={!approvalIdentityReady || busy === "reject"}
          onClick={() => onDecision(false)}
        >
          {remediation ? "Reject retraction" : "Reject recommendation"}
        </Button>
        <Button
          id="approveRun"
          type="button"
          variant="primary"
          data-operator
          disabled={!approvalIdentityReady || busy === "approve"}
          onClick={() => onDecision(true)}
          {...currentControl(walkthroughStep === "review" ? "review" : "reanalyze")}
        >
          {remediation ? "Approve retraction" : "Approve recommendation"}
        </Button>
      </div>

      <div className="rewind-console">
        <div>
          <p className="section-kicker">Audited correction</p>
          <h3 id="rewindTitle">Rewind belief state</h3>
        </div>
        <div className="field">
          <label htmlFor="rewindTimestamp">Restore beliefs as of</label>
          <input
            id="rewindTimestamp"
            type="datetime-local"
            step="1"
            value={rewindTimestamp}
            onChange={(event) => onRewindTimestamp(event.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="rewindReason">Reason</label>
          <input
            id="rewindReason"
            value={rewindReason}
            onChange={(event) => onRewindReason(event.target.value)}
          />
        </div>
        <div className="rewind-actions">
          <Button
            id="previewRewind"
            type="button"
            data-operator
            disabled={busy === "preview"}
            onClick={onPreview}
            {...currentControl("preview")}
          >
            {busy === "preview" ? "Previewing" : "Preview"}
          </Button>
          <Button
            id="executeRewind"
            type="button"
            variant="danger"
            data-operator
            disabled={!rewindPreview || busy === "execute"}
            onClick={onExecute}
            {...currentControl("execute")}
          >
            {busy === "execute" ? "Rewinding" : "Execute rewind"}
          </Button>
        </div>
        <div id="rewindPreview" className="rewind-preview" role="status" aria-live="polite">
          {rewindPreview ? (
            <>
              <strong>
                {rewindPreview.effect_payload?.close_memory_ids
                  ? `${rewindPreview.effect_payload.close_memory_ids.length} versions will close.`
                  : "Close count unavailable."}
              </strong>
              <span>
                {rewindPreview.effect_payload?.reassertions
                  ? `${rewindPreview.effect_payload.reassertions.length} historical beliefs will be reasserted as audited versions.`
                  : "Reassertion count unavailable."}{" "}
                Preview expires {formatTime(rewindPreview.expires_at)}.
              </span>
            </>
          ) : (
            <>
              <Warning aria-hidden="true" size={18} />
              <span>Choose a point on the belief timeline to preview its impact.</span>
            </>
          )}
        </div>
      </div>
    </section>
  );
}
