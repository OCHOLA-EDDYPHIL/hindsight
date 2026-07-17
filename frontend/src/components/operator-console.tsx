import { LockKey, SignOut, Warning } from "@phosphor-icons/react";
import { FormEvent, useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { formatTime, humanStatus } from "@/lib/format";
import type { Incident, RewindPreview, Run } from "@/types";

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

const phases = ["triage", "recall", "plan", "approval", "reflection"];

export function OperatorConsole({
  operator,
  incidents,
  incident,
  run,
  incidentInput,
  busy,
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
  const eventPhases = new Set((run?.events || []).map((event) => event.phase));
  const currentPhase = run?.events?.at(-1)?.phase;
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
        <Button id="lockButton" type="button" variant="ghost" size="compact" onClick={onLock}>
          <SignOut aria-hidden="true" size={14} />
          Return to read-only
        </Button>
      </header>

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
          >
            {busy === "poison" ? "Injecting" : "Inject poison"}
          </Button>
          <Button
            id="startRun"
            type="button"
            variant="primary"
            data-operator
            disabled={!operator || busy === "run"}
            onClick={onRun}
          >
            {busy === "run" ? "Queuing" : "Analyze incident"}
          </Button>
        </div>
      </div>

      <ol id="phaseRail" className="phase-rail" aria-label="Agent run phases">
        {phases.map((phase) => {
          const complete = eventPhases.has(phase) && phase !== currentPhase;
          const active = phase === currentPhase && !run?.status.match(/completed|rejected|failed/);
          return (
            <li key={phase} data-phase={phase} className={complete ? "complete" : active ? "active" : ""}>
              <span aria-hidden="true" />
              {phase}
            </li>
          );
        })}
      </ol>

      <div id="approvalActions" className="approval-actions" hidden={run?.status !== "awaiting_approval"}>
        <div>
          <strong>Decision awaits operator review</strong>
          <span>{run?.decision_id || "identity pending"}</span>
        </div>
        <Button
          id="rejectRun"
          type="button"
          variant="danger"
          data-operator
          disabled={!operator || busy === "reject"}
          onClick={() => onDecision(false)}
        >
          Reject
        </Button>
        <Button
          id="approveRun"
          type="button"
          variant="primary"
          data-operator
          disabled={!operator || busy === "approve"}
          onClick={() => onDecision(true)}
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
