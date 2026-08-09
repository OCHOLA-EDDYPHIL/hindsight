import { ListChecks, LockKey, SignOut, Warning } from "@phosphor-icons/react";
import { FormEvent, useEffect, useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { formatTime, humanStatus } from "@/lib/format";
import type { Incident, RewindPreview, Run, SignatureScenario, Snapshot } from "@/types";

export function OperatorAccess({
  open,
  onOpenChange,
  operator,
  onUnlock,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  operator: boolean;
  onUnlock: (token: string) => Promise<boolean>;
}) {
  const [token, setToken] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    const unlocked = await onUnlock(token);
    setSubmitting(false);
    if (unlocked) {
      setToken("");
      onOpenChange(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>
        <Button id="operatorButton" type="button" variant={operator ? "primary" : "quiet"}>
          <LockKey aria-hidden="true" size={15} weight="bold" />
          <span id="operatorLabel">{operator ? "Operator" : "Operator access"}</span>
        </Button>
      </DialogTrigger>
      <DialogContent id="operatorPanel" aria-label="Operator access">
        <DialogTitle className="text-xl font-semibold text-text">Unlock mutation controls</DialogTitle>
        <DialogDescription className="mt-2 max-w-[52ch] text-sm leading-6 text-muted">
          Public replay is credential free. Model calls, memory injection, approvals, and rewind
          execution require a passcode-backed session.
        </DialogDescription>
        <p className="mt-3 text-xs leading-5 text-muted">
          Unlock first, then use the optional walkthrough to reset, import stale guidance, analyze,
          correct the belief state, and inspect history.
        </p>
        <form id="operatorForm" className="mt-6 grid gap-2" onSubmit={submit}>
          <label className="text-sm font-semibold text-text" htmlFor="operatorToken">
            Operator passcode
          </label>
          <input
            id="operatorToken"
            name="token"
            type="password"
            autoComplete="current-password"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            required
          />
          <p className="form-help">The passcode is exchanged for a secure, same-origin session.</p>
          <Button className="mt-3 w-full" type="submit" variant="primary" disabled={submitting}>
            {submitting ? "Verifying" : "Unlock controls"}
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  );
}

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
    id: "unlock",
    label: "Unlock controls",
    detail: "Use Operator access to exchange the passcode for a protected session.",
  },
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
  operator,
  rewindAnchor,
  scenario,
  snapshot,
  run,
  rewindPreview,
}: {
  operator: boolean;
  rewindAnchor: string | null;
  scenario: SignatureScenario | null;
  snapshot: Snapshot | null;
  run: Run | null;
  rewindPreview: RewindPreview | null;
}): WalkthroughStep {
  if (!operator) return "unlock";
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
  if (run?.status !== "completed") return "reanalyze";
  return "history";
}

export function OperatorConsole({
  operator,
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
  onLock,
}: {
  operator: boolean;
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
  onLock: () => void;
}) {
  const [walkthroughOpen, setWalkthroughOpen] = useState(false);
  const eventPhases = new Set((run?.events || []).map((event) => event.phase));
  const currentPhase = run?.events?.at(-1)?.phase;
  const walkthroughStep = deriveWalkthroughStep({
    operator,
    rewindAnchor,
    scenario,
    snapshot,
    run,
    rewindPreview,
  });
  const walkthroughIndex = walkthroughSteps.findIndex((step) => step.id === walkthroughStep);
  const recommendationId = run?.action_trace?.recommendation?.id;
  const selectionFingerprint = run?.action_trace?.selection?.fingerprint;
  const approvalIdentityReady = Boolean(recommendationId && selectionFingerprint);
  const describedBy = walkthroughOpen ? "walkthroughCurrent" : undefined;
  const currentControl = (step: WalkthroughStep) =>
    walkthroughOpen && walkthroughStep === step
      ? { "aria-describedby": describedBy, "data-walkthrough-current": "true" }
      : {};
  useEffect(() => {
    if (!operator) setWalkthroughOpen(false);
  }, [operator]);
  return (
    <section
      className="operator-console"
      hidden={!operator}
      aria-label="Protected operator controls"
      aria-hidden={!operator}
    >
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
          <Button id="lockButton" type="button" variant="ghost" size="compact" onClick={onLock}>
            <SignOut aria-hidden="true" size={14} />
            Return to read-only
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
            disabled={!operator || busy === "reset"}
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
            disabled={!operator || busy === "poison"}
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
            disabled={!operator || busy === "run"}
            onClick={onRun}
            {...currentControl(walkthroughStep === "reanalyze" ? "reanalyze" : "analyze")}
          >
            {busy === "run" ? "Queuing" : "Analyze incident"}
          </Button>
        </div>
      </div>

      <ol id="phaseRail" className="phase-rail" aria-label="Agent run phases">
        {phases.map((phase) => {
          const complete = eventPhases.has(phase.key) && phase.key !== currentPhase;
          const active =
            phase.key === currentPhase && !run?.status.match(/completed|rejected|failed/);
          return (
            <li
              key={phase.key}
              data-phase={phase.key}
              className={complete ? "complete" : active ? "active" : ""}
            >
              <span aria-hidden="true" />
              {phase.label}
            </li>
          );
        })}
      </ol>

      <div id="approvalActions" className="approval-actions" hidden={run?.status !== "awaiting_approval"}>
        <div>
          <strong>Decision awaits operator review</strong>
          <span>
            {approvalIdentityReady
              ? run?.action_trace?.recommendation?.summary || "Recommendation identity verified"
              : "Approval identity unavailable. Refresh or rerun the analysis."}
          </span>
        </div>
        <Button
          id="rejectRun"
          type="button"
          variant="danger"
          data-operator
          disabled={!operator || !approvalIdentityReady || busy === "reject"}
          onClick={() => onDecision(false)}
        >
          Reject recommendation
        </Button>
        <Button
          id="approveRun"
          type="button"
          variant="primary"
          data-operator
          disabled={!operator || !approvalIdentityReady || busy === "approve"}
          onClick={() => onDecision(true)}
          {...currentControl(walkthroughStep === "review" ? "review" : "reanalyze")}
        >
          Approve recommendation
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
            disabled={!operator || busy === "preview"}
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
            disabled={!operator || !rewindPreview || busy === "execute"}
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
                {(rewindPreview.effect_payload?.close_memory_ids || []).length} versions will close.
              </strong>
              <span>
                {(rewindPreview.effect_payload?.reassertions || []).length} historical beliefs will
                be reasserted as audited versions. Preview expires {formatTime(rewindPreview.expires_at)}.
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
