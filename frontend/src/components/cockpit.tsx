import {
  ArrowClockwise,
  ArrowRight,
  Check,
  ClockCounterClockwise,
  Copy,
  Fingerprint,
  Flask,
  LockKey,
  Pulse,
  ShieldCheck,
  Warning,
} from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SafeMarkdown } from "@/components/safe-markdown";
import { humanStatus, formatTime, shortId, structurePlan } from "@/lib/format";
import { cn } from "@/lib/utils";
import type {
  Incident,
  InfluenceItem,
  MemoryOperation,
  MemoryRecord,
  Run,
  SignatureScenario,
  Snapshot,
} from "@/types";

export function IdentifierValue({
  value,
  label,
  quiet = false,
}: {
  value?: string | null;
  label: string;
  quiet?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (!value) return;
    await navigator.clipboard.writeText(value);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };
  return (
    <button
      type="button"
      className={cn(
        "group inline-flex min-w-0 items-center gap-2 font-mono text-[11px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent",
        quiet ? "text-muted" : "text-text",
      )}
      onClick={copy}
      disabled={!value}
      title={value || `${label} pending`}
      aria-label={value ? `Copy ${label}: ${value}` : `${label} pending`}
    >
      <span className="truncate">{shortId(value)}</span>
      {copied ? (
        <Check className="shrink-0 text-accent" aria-hidden="true" size={13} weight="bold" />
      ) : (
        <Copy
          className="shrink-0 opacity-45 transition-opacity group-hover:opacity-100"
          aria-hidden="true"
          size={13}
        />
      )}
    </button>
  );
}

export function ConnectionState({ state }: { state: string }) {
  const label =
    state === "live"
      ? "Live"
      : state === "historical"
        ? "Historical"
        : state === "reconnecting"
          ? "Reconnecting"
          : state === "disconnected"
            ? "Disconnected"
            : "Connecting";
  return (
    <span
      id="connection"
      role="status"
      aria-live="polite"
      className={cn(
        "inline-flex min-h-8 items-center gap-2 border px-3 font-mono text-[11px] font-semibold",
        state === "live" && "border-accent/50 text-accent",
        state === "historical" && "border-warning/50 text-warning",
        ["connecting", "reconnecting"].includes(state) && "border-line-strong text-muted",
        state === "disconnected" && "border-danger/50 text-danger",
      )}
    >
      <Pulse
        aria-hidden="true"
        className={cn(state === "reconnecting" && "motion-safe:animate-pulse")}
        size={14}
        weight="bold"
      />
      <span>{label}</span>
    </span>
  );
}

const STAGE_META = [
  { key: "baseline_memory_id", label: "Baseline", kind: "memory" },
  { key: "poison_memory_id", label: "Poisoned memory", kind: "memory" },
  { key: "influenced_decision_id", label: "Influenced decision", kind: "decision" },
  { key: "rewind_operation_id", label: "Audited rewind", kind: "operation" },
  { key: "corrected_decision_id", label: "Corrected decision", kind: "decision" },
] as const;

export function CausalRail({ scenario }: { scenario: SignatureScenario | null }) {
  return (
    <section className="causal-rail" aria-labelledby="causalHeading">
      <div className="rail-heading">
        <div>
          <p className="section-kicker">Causal chain</p>
          <h2 id="causalHeading">One incident. Every durable identity.</h2>
        </div>
        <IdentifierValue value={scenario?.scenario_id} label="scenario identity" quiet />
      </div>
      <ol aria-label="Signature replay chronology">
        {STAGE_META.map((stage, index) => {
          const value = scenario?.stages[stage.key];
          return (
            <li
              key={stage.key}
              data-stage={stage.key}
              className={cn(value && "resolved")}
              style={{ "--stage-index": index } as React.CSSProperties}
            >
              <span className="stage-index" aria-hidden="true">
                {String(index + 1).padStart(2, "0")}
              </span>
              <span className="stage-copy">
                <strong>{stage.label}</strong>
                <span>{stage.kind}</span>
              </span>
              <IdentifierValue value={value} label={`${stage.label} identity`} quiet />
              {index < STAGE_META.length - 1 ? (
                <ArrowRight className="stage-arrow" aria-hidden="true" size={16} />
              ) : null}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function PlanSections({ run, primary = false }: { run?: Run | null; primary?: boolean }) {
  const plan = structurePlan(run);
  return (
    <div id={primary ? "planText" : undefined} className="plan-sections">
      <div>
        <span className="plan-label">Cause</span>
        <SafeMarkdown>{plan.cause}</SafeMarkdown>
      </div>
      <div>
        <span className="plan-label">Checks</span>
        <SafeMarkdown>{plan.checks}</SafeMarkdown>
      </div>
      <div>
        <span className="plan-label">Action</span>
        <SafeMarkdown
          id={primary ? "proposedAction" : undefined}
          className="proposed-action"
        >
          {plan.action}
        </SafeMarkdown>
      </div>
      <div>
        <span className="plan-label">Safety</span>
        <p>{plan.safety}</p>
      </div>
    </div>
  );
}

function Outcome({ run, mode }: { run?: Run | null; mode: "historical" | "current" }) {
  const historical = mode === "historical";
  return (
    <article className={cn("outcome", historical ? "outcome-historical" : "outcome-current")}>
      <header>
        <div>
          <Badge tone={historical ? "historical" : "current"}>
            {historical ? "Historical outcome" : "Current outcome"}
          </Badge>
          <h3>{historical ? "Rejected recommendation" : "Corrected recommendation"}</h3>
        </div>
        <span className="outcome-status">{humanStatus(run?.status)}</span>
      </header>
      <PlanSections run={run} primary={!historical} />
      <footer>
        <span>decision</span>
        <IdentifierValue value={run?.decision_id} label={`${mode} decision`} />
        <span>{formatTime(run?.completed_at || run?.created_at)}</span>
      </footer>
    </article>
  );
}

export function OutcomeComparison({
  scenario,
  activeRun,
}: {
  scenario: SignatureScenario | null;
  activeRun: Run | null;
}) {
  const rejected = scenario?.runs.find((item) => item.status === "rejected") ||
    (activeRun?.status === "rejected" ? activeRun : null);
  const corrected = [...(scenario?.runs || [])]
    .reverse()
    .find((item) => item.status === "completed") ||
    (activeRun?.status !== "rejected" ? activeRun : null);
  return (
    <section className="comparison" aria-labelledby="comparisonHeading">
      <div className="comparison-heading">
        <div>
          <p className="section-kicker">Decision delta</p>
          <h2 id="comparisonHeading">The memory changed. The plan changed.</h2>
        </div>
        <div className="delta-key" aria-label="Outcome chronology">
          <ClockCounterClockwise aria-hidden="true" size={16} />
          <span>Past is retained</span>
          <ArrowRight aria-hidden="true" size={14} />
          <ShieldCheck aria-hidden="true" size={16} />
          <span>Current is governed</span>
        </div>
      </div>
      <div className="outcome-grid">
        <Outcome run={rejected} mode="historical" />
        <Outcome run={corrected} mode="current" />
      </div>
    </section>
  );
}

function MemoryRow({ memory }: { memory: MemoryRecord }) {
  const invalid = memory.status === "invalidated" || Boolean(memory.t_invalid);
  const review = memory.trust_status === "review_required";
  return (
    <article
      className={cn("memory", invalid && "invalidated")}
      data-memory-id={memory.id}
      title={memory.content || `Memory ${memory.id}`}
    >
      <span className="memory-state" aria-hidden="true" />
      <span className="memory-body">
        <strong className="memory-content">
          {memory.content || `${memory.content_schema || "governed memory"} / ${shortId(memory.id)}`}
        </strong>
        <span className="memory-meta">
          <span className="memory-status">
            {invalid ? "invalidated" : review ? "review required" : "current"}
          </span>
          <span>{memory.writer || "writer unavailable"}</span>
          <span>{formatTime(memory.written_at || memory.t_valid)}</span>
        </span>
      </span>
      <IdentifierValue value={memory.belief_id || memory.id} label="belief identity" quiet />
    </article>
  );
}

export function BeliefLedger({ snapshot }: { snapshot: Snapshot | null }) {
  const memories = snapshot?.memories || [];
  const current = memories.filter(
    (memory) => memory.status !== "invalidated" && !memory.t_invalid,
  );
  const invalid = memories.length - current.length;
  return (
    <section className="evidence-pane" aria-labelledby="beliefTitle">
      <header className="pane-heading">
        <div>
          <p className="section-kicker">Belief ledger</p>
          <h2 id="beliefTitle">{snapshot?.as_of ? "Beliefs As Of" : "Current Beliefs"}</h2>
        </div>
        <span id="memoryCount" className="metric">
          {current.length} live · {invalid} invalid
        </span>
      </header>
      <div id="memories" className="memory-list" aria-live="polite">
        {memories.length ? (
          memories.map((memory) => <MemoryRow key={memory.id} memory={memory} />)
        ) : (
          <div className="empty-inline">
            <Fingerprint aria-hidden="true" size={22} />
            <div>
              <strong>No beliefs in this state</strong>
              <p>A signature replay will populate durable versions and provenance here.</p>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

export function InfluenceLedger({ influence }: { influence: InfluenceItem[] }) {
  return (
    <section className="influence-pane" aria-labelledby="influenceTitle">
      <header className="pane-heading">
        <div>
          <p className="section-kicker">Decision evidence</p>
          <h2 id="influenceTitle">Cited memory reads</h2>
        </div>
        <span id="influenceCount" className="metric">
          {influence.length} read{influence.length === 1 ? "" : "s"}
        </span>
      </header>
      <div id="influenceList" className="influence-list">
        {influence.length ? (
          influence.map((item, index) => {
            const memory = item.memory;
            return (
              <article key={item.read?.id || memory?.id || index}>
                <div className="influence-rank">{String(index + 1).padStart(2, "0")}</div>
                <div>
                  <strong>{memory?.content || "Memory content unavailable"}</strong>
                  <p>
                    {item.provenance?.writer || memory?.writer || "writer unavailable"} / rank{" "}
                    {item.read?.rank ?? index + 1}
                  </p>
                </div>
                <IdentifierValue value={memory?.id} label="memory identity" quiet />
              </article>
            );
          })
        ) : (
          <div className="empty-inline compact">
            <Flask aria-hidden="true" size={20} />
            <div>
              <strong>No reads selected</strong>
              <p>Select or run a decision to inspect its cited memory.</p>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

export function Timeline({
  snapshot,
  onSelect,
}: {
  snapshot: Snapshot | null;
  onSelect: (value?: string | null) => void;
}) {
  const timeline = snapshot?.timeline || [];
  const timelineRef = useRef<HTMLInputElement>(null);
  const selectedIndex = snapshot?.as_of
    ? Math.max(timeline.indexOf(snapshot.as_of), 0)
    : Math.max(timeline.length - 1, 0);
  useEffect(() => {
    const element = timelineRef.current;
    if (!element) return;
    const select = () => {
      const value = timeline[Number(element.value)];
      if (value) onSelect(value);
    };
    element.addEventListener("input", select);
    return () => element.removeEventListener("input", select);
  }, [onSelect, timeline]);
  return (
    <section className="timeline" aria-label="Belief history">
      <div className="timeline-label">
        <ClockCounterClockwise aria-hidden="true" size={18} />
        <div>
          <label htmlFor="timeline">Belief state</label>
          <output id="timeLabel" htmlFor="timeline">
            {snapshot?.as_of ? `As of ${formatTime(snapshot.as_of)}` : "Live belief state"}
          </output>
        </div>
      </div>
      <input
        ref={timelineRef}
        id="timeline"
        type="range"
        min={0}
        max={Math.max(timeline.length - 1, 0)}
        value={selectedIndex}
        onChange={() => undefined}
        disabled={!timeline.length}
        aria-label="Inspect a historical belief state"
      />
      <Button id="liveButton" type="button" size="compact" variant="ghost" onClick={() => onSelect(null)}>
        <ArrowClockwise aria-hidden="true" size={14} />
        Return to live
      </Button>
    </section>
  );
}

function OperationRow({ operation }: { operation: MemoryOperation }) {
  const reviewCount = (operation.effects || []).filter(
    (effect) => effect.effect_type === "review_required",
  ).length;
  return (
    <article
      className={cn("operation", `operation-${operation.status}`)}
      data-operation-id={operation.id}
      data-operation-type={operation.operation_type}
      data-operation-status={operation.status}
    >
      <div>
        <strong>
          {operation.operation_type} · {operation.status}
        </strong>
        <span>{formatTime(operation.completed_at || operation.created_at)}</span>
      </div>
      <p>{operation.reason || "No operation reason recorded."}</p>
      <span>
        closed {(operation.invalidated_memory_ids || []).length} / restored{" "}
        {(operation.restored_memory_ids || []).length}
        {reviewCount ? ` / review ${reviewCount}` : ""}
      </span>
      {operation.failure_detail ? <p className="operation-failure">{operation.failure_detail}</p> : null}
    </article>
  );
}

export function OperationLedger({ operations }: { operations: MemoryOperation[] }) {
  return (
    <section className="operations-pane" aria-labelledby="operationsHeading">
      <header className="pane-heading">
        <div>
          <p className="section-kicker">Mutation audit</p>
          <h2 id="operationsHeading">Memory operations</h2>
        </div>
        <span id="operationCount" className="metric">
          {operations.length} operation{operations.length === 1 ? "" : "s"}
        </span>
      </header>
      <div id="operations" className="operation-list" aria-live="polite">
        {operations.length ? (
          [...operations].reverse().map((operation) => (
            <OperationRow key={operation.id} operation={operation} />
          ))
        ) : (
          <div className="empty-inline compact">
            <ShieldCheck aria-hidden="true" size={20} />
            <div>
              <strong>No mutation recorded</strong>
              <p>Previews remain separate until an operator executes an audited operation.</p>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

export function StoryHeader({
  incident,
  namespace,
  run,
}: {
  incident: Incident | null;
  namespace: string;
  run: Run | null;
}) {
  return (
    <section className="story-header" aria-labelledby="incidentHeading">
      <div className="story-copy">
        <p className="section-kicker">Guided signature replay</p>
        <h1 id="incidentHeading">{incident?.title || "Governed memory, inspected end to end"}</h1>
        <p id="incidentSummary">
          {incident?.summary ||
            "Follow a poisoned belief through decision influence, rewind, and a corrected outcome."}
        </p>
      </div>
      <dl className="story-facts">
        <div>
          <dt>Severity</dt>
          <dd id="incidentSeverity">{incident?.severity || "not recorded"}</dd>
        </div>
        <div>
          <dt>Service</dt>
          <dd id="incidentService">{incident?.service_slug || "not recorded"}</dd>
        </div>
        <div className="fact-wide">
          <dt>Namespace</dt>
          <dd id="namespace" title={namespace}>{namespace}</dd>
        </div>
        <div>
          <dt>Run</dt>
          <dd id="runStatus">{run ? humanStatus(run.status) : "No run"}</dd>
        </div>
      </dl>
    </section>
  );
}

export function LoadingSurface() {
  return (
    <div className="loading-surface" role="status" aria-live="polite">
      <span className="sr-only">Loading governed memory replay</span>
      <div className="skeleton-line wide" />
      <div className="skeleton-line" />
      <div className="skeleton-grid">
        {Array.from({ length: 5 }, (_, index) => (
          <div className="skeleton-cell" key={index} />
        ))}
      </div>
      <div className="skeleton-comparison">
        <div />
        <div />
      </div>
    </div>
  );
}

export function EmptySurface({ onOperator }: { onOperator: () => void }) {
  return (
    <section className="state-surface" aria-labelledby="emptyHeading">
      <Fingerprint aria-hidden="true" size={32} />
      <p className="section-kicker">Replay unavailable</p>
      <h1 id="emptyHeading">No completed signature story is ready yet.</h1>
      <p>
        Public replay begins only after a rejected decision, audited rewind, and corrected decision form
        one coherent trace.
      </p>
      <Button type="button" variant="primary" onClick={onOperator}>
        <LockKey aria-hidden="true" size={16} weight="bold" />
        Operator access
      </Button>
    </section>
  );
}

export function ErrorSurface({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <section className="state-surface error-surface" role="alert" aria-labelledby="errorHeading">
      <Warning aria-hidden="true" size={32} />
      <p className="section-kicker">Trace interrupted</p>
      <h1 id="errorHeading">The replay could not be resolved.</h1>
      <p>{message}</p>
      <Button type="button" variant="quiet" onClick={onRetry}>
        <ArrowClockwise aria-hidden="true" size={16} />
        Retry trace
      </Button>
    </section>
  );
}
