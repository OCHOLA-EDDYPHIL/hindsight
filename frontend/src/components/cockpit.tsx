import {
  ArrowClockwise,
  ArrowRight,
  Check,
  ClockCounterClockwise,
  Copy,
  Fingerprint,
  Flask,
  Pulse,
  ShieldCheck,
  SignIn,
  Warning,
} from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SafeMarkdown } from "@/components/safe-markdown";
import { humanStatus, formatTime, shortId, structurePlan } from "@/lib/format";
import { cn } from "@/lib/utils";
import type {
  CausalEnvelope,
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
  if (!value) {
    return <span className="identifier-unavailable">Unavailable</span>;
  }
  const copy = async () => {
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
      title={value}
      aria-label={`Copy ${label}: ${value}`}
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

export function rejectedRun(scenario: SignatureScenario | null, activeRun?: Run | null) {
  const stageId = scenario?.stages.influenced_decision_id;
  return (
    scenario?.runs.find((item) => stageId && item.decision_id === stageId) ||
    scenario?.runs.find((item) => item.status === "rejected") ||
    (activeRun?.status === "rejected" ? activeRun : null)
  );
}

export function postCorrectionRun(scenario: SignatureScenario | null, activeRun?: Run | null) {
  const stageId = scenario?.stages.corrected_decision_id;
  return (
    scenario?.runs.find((item) => stageId && item.decision_id === stageId) ||
    activeRun ||
    scenario?.runs.at(-1) ||
    null
  );
}

function recommendationText(run?: Run | null) {
  return (
    run?.proposed_action?.trim() ||
    run?.action_trace?.recommendation?.summary?.trim() ||
    run?.plan?.trim() ||
    null
  );
}

function runApproval(run?: Run | null): boolean | null {
  if (typeof run?.action_approved === "boolean") return run.action_approved;
  const approved = run?.action_trace?.approval?.approved;
  return typeof approved === "boolean" ? approved : null;
}

function decisionDisposition(
  run: Run | null | undefined,
  mode: "rejected" | "post",
  correctionProven = false,
) {
  if (!run) return "Unavailable";
  const approved = runApproval(run);
  if (mode === "rejected") {
    if (approved === false) return "Rejected by operator";
    if (approved === true) return "Approved; run later rejected";
    return run.status === "rejected"
      ? "Run rejected; approval unavailable"
      : humanStatus(run.status);
  }
  if (approved === true && correctionProven) return "Approved after correction";
  if (approved === true) return "Approved by operator";
  if (approved === false) return "Rejected by operator";
  return run.status === "completed"
    ? "Run completed; approval unavailable"
    : humanStatus(run.status);
}

type CausalEvidenceState =
  | "changed"
  | "unchanged"
  | "unavailable"
  | "mismatched"
  | "corrected-only";

export function boundCausalEnvelope(
  run: Run | null | undefined,
  expectedSha256?: string | null,
): CausalEnvelope | null {
  const envelope = run?.action_trace?.causal_envelope;
  return expectedSha256 && envelope?.envelope_sha256 === expectedSha256 ? envelope : null;
}

export function evidenceState(scenario: SignatureScenario | null): CausalEvidenceState {
  const evidence = scenario?.causal_evidence;
  const comparison = scenario?.action_comparison;
  const before = boundCausalEnvelope(rejectedRun(scenario), evidence?.before_envelope_sha256);
  const after = boundCausalEnvelope(postCorrectionRun(scenario), evidence?.after_envelope_sha256);
  if (
    !scenario?.stages.influenced_decision_id &&
    !comparison?.before &&
    Boolean(after)
  ) {
    return "corrected-only";
  }

  const checks = evidence?.controlled_pair_checks || [];
  if (
    checks.some((check) => check.status === "mismatched") ||
    comparison?.context.prompt_equal === false ||
    comparison?.context.normalized_telemetry_equal === false
  ) {
    return "mismatched";
  }
  const allChecksMatched = checks.length > 0 && checks.every((check) => check.status === "matched");
  const pairProven = evidence?.proof_states.controlled_pair_eligible.status === "proven";
  if (
    !before ||
    !after ||
    !comparison?.before ||
    !comparison.after ||
    comparison.status === "unavailable" ||
    !allChecksMatched ||
    !pairProven
  ) {
    return "unavailable";
  }
  if (comparison.status === "unchanged") return "unchanged";
  return comparison.controlled_pair && evidence?.proof_states.action_delta_proven.status === "proven"
    ? "changed"
    : "unavailable";
}

function causalClaim(scenario: SignatureScenario | null) {
  const state = evidenceState(scenario);
  if (state === "changed") {
    return "Recorded action changed after correction.";
  }
  if (state === "unchanged") {
    return "Active memory changed; primary action did not.";
  }
  if (state === "mismatched") {
    return "Action comparison withheld; invariants differ.";
  }
  if (state === "corrected-only") {
    return "Corrected recommendation recorded; before evidence unavailable.";
  }
  return "Causal comparison unavailable.";
}

function findCitedRead(run: Run | null | undefined, memoryId?: string | null) {
  if (!memoryId) return null;
  return run?.trace?.reads?.find((read) => read.memory_id === memoryId) || null;
}

function findMemory(
  memoryId: string | null | undefined,
  scenario: SignatureScenario | null,
  snapshot: Snapshot | null,
) {
  if (!memoryId) return null;
  return (
    snapshot?.memories.find((memory) => memory.id === memoryId) ||
    scenario?.memories.find((memory) => memory.id === memoryId) ||
    null
  );
}

function CausalCard({
  step,
  kind,
  dataStage,
  title,
  status,
  content,
  identity,
  identityLabel,
  details,
  last = false,
}: {
  step: string;
  kind: string;
  dataStage: string;
  title: string;
  status: string;
  content?: string | null;
  identity?: string | null;
  identityLabel: string;
  details: Array<{ label: string; value?: string | number | null }>;
  last?: boolean;
}) {
  return (
    <li className={cn(identity && "resolved")} data-stage={dataStage}>
      <div className="causal-card-heading">
        <span>{step} / {kind}</span>
        <strong>{status}</strong>
      </div>
      <h3>{title}</h3>
      <div className="causal-recorded">
        <span>Recorded evidence</span>
        {content ? <SafeMarkdown>{content}</SafeMarkdown> : <p>Unavailable</p>}
      </div>
      <dl>
        {details.map((detail) => (
          <div key={detail.label}>
            <dt>{detail.label}</dt>
            <dd>
              {detail.value === null || detail.value === undefined || detail.value === ""
                ? "Unavailable"
                : detail.value}
            </dd>
          </div>
        ))}
      </dl>
      <footer>
        <span>Recorded identity</span>
        <IdentifierValue value={identity} label={identityLabel} quiet />
      </footer>
      {!last ? <ArrowRight className="stage-arrow" aria-hidden="true" size={18} /> : null}
    </li>
  );
}

export function CausalRail({
  scenario,
  snapshot,
  activeRun,
}: {
  scenario: SignatureScenario | null;
  snapshot: Snapshot | null;
  activeRun: Run | null;
}) {
  const rejected = rejectedRun(scenario, activeRun);
  const latestRun = postCorrectionRun(scenario, activeRun);
  const compromisedId =
    scenario?.stages.compromised_memory_id || scenario?.stages.poison_memory_id;
  const compromised = findMemory(compromisedId, scenario, snapshot);
  const citedRead = findCitedRead(rejected, compromisedId);
  const operation =
    scenario?.operation ||
    snapshot?.operations.find((item) => item.id === scenario?.stages.rewind_operation_id) ||
    null;
  const correctedStageId = scenario?.stages.corrected_decision_id;
  const postCorrection = correctedStageId
    ? latestRun
    : operation?.status === "completed" &&
        latestRun?.decision_id &&
        latestRun.decision_id !== scenario?.stages.influenced_decision_id
      ? latestRun
      : null;
  const correctionCounts = operation
    ? [
        operation.invalidated_memory_ids
          ? `${operation.invalidated_memory_ids.length} version${operation.invalidated_memory_ids.length === 1 ? "" : "s"} closed`
          : null,
        operation.restored_memory_ids
          ? `${operation.restored_memory_ids.length} version${operation.restored_memory_ids.length === 1 ? "" : "s"} restored`
          : null,
      ].filter(Boolean).join(" · ") || null
    : null;
  return (
    <section className="causal-rail" aria-labelledby="causalHeading">
      <div className="rail-heading">
        <div>
          <p className="section-kicker">Before → correction → after</p>
          <h2 id="causalHeading">{causalClaim(scenario)}</h2>
        </div>
        <IdentifierValue value={scenario?.scenario_id} label="scenario identity" quiet />
      </div>
      <ol aria-label="Signature replay chronology">
        <CausalCard
          step="01"
          kind="memory read"
          dataStage="compromised_memory_id"
          title="Cited belief"
          status={
            citedRead
              ? compromised?.t_invalid || compromised?.status === "invalidated"
                ? "Read; now invalidated"
                : "Read by agent"
              : compromised
                ? "Belief recorded; read unavailable"
                : "Unavailable"
          }
          content={compromised?.content || citedRead?.justification}
          identity={compromisedId}
          identityLabel="cited belief"
          details={[
            { label: "Writer", value: citedRead?.writer || compromised?.writer },
            { label: "Source", value: citedRead?.source_ref || compromised?.source_ref },
          ]}
        />
        <CausalCard
          step="02"
          kind="decision"
          dataStage="influenced_decision_id"
          title="Pre-correction recommendation"
          status={decisionDisposition(rejected, "rejected")}
          content={recommendationText(rejected)}
          identity={scenario?.stages.influenced_decision_id || rejected?.decision_id}
          identityLabel="pre-correction decision"
          details={[
            {
              label: "Provider",
              value: rejected?.provider || rejected?.action_trace?.selection?.provider,
            },
            { label: "Model", value: rejected?.model || rejected?.action_trace?.selection?.model },
            {
              label: "Primary action",
              value: scenario?.action_comparison?.before?.primary_action,
            },
          ]}
        />
        <CausalCard
          step="03"
          kind="correction"
          dataStage="rewind_operation_id"
          title="Audited rewind"
          status={operation ? humanStatus(operation.status) : "Unavailable"}
          content={operation?.reason}
          identity={scenario?.stages.rewind_operation_id || operation?.id}
          identityLabel="rewind operation"
          details={[
            { label: "Effect", value: correctionCounts },
            { label: "Completed", value: operation?.completed_at ? formatTime(operation.completed_at) : null },
          ]}
        />
        <CausalCard
          step="04"
          kind="rerun"
          dataStage="corrected_decision_id"
          title={
            scenario?.status === "completed"
              ? "Post-correction recommendation"
              : "Rerun recommendation"
          }
          status={decisionDisposition(
            postCorrection,
            "post",
            Boolean(scenario?.action_comparison?.memory_correction_proven),
          )}
          content={recommendationText(postCorrection)}
          identity={scenario?.stages.corrected_decision_id || postCorrection?.decision_id}
          identityLabel="post-correction decision"
          details={[
            {
              label: "Provider",
              value:
                postCorrection?.provider || postCorrection?.action_trace?.selection?.provider,
            },
            {
              label: "Model",
              value: postCorrection?.model || postCorrection?.action_trace?.selection?.model,
            },
            {
              label: "Primary action",
              value: scenario?.action_comparison?.after?.primary_action,
            },
          ]}
          last
        />
      </ol>
    </section>
  );
}

function PlanSections({ run, primary = false }: { run?: Run | null; primary?: boolean }) {
  const plan = structurePlan(run);
  const approval = run
    ? decisionDisposition(run, run.status === "rejected" ? "rejected" : "post")
    : "Unavailable";
  const fields = [
    { label: "Recorded plan", value: plan.recordedPlan },
    { label: "Cause", value: plan.cause },
    { label: "Checks", value: plan.checks },
    { label: "Action", value: plan.action, id: primary ? "proposedAction" : undefined },
  ];
  return (
    <div id={primary ? "planText" : undefined} className="plan-sections">
      {fields.map((field) => (
        <div key={field.label}>
          <span className="plan-label">{field.label}</span>
          {field.value ? (
            <SafeMarkdown
              id={field.id}
              className={field.label === "Action" ? "proposed-action" : undefined}
            >
              {field.value}
            </SafeMarkdown>
          ) : (
            <p id={field.id} className="unavailable-value">Unavailable</p>
          )}
        </div>
      ))}
      <div>
        <span className="plan-label">Approval outcome</span>
        <p>{approval}</p>
      </div>
    </div>
  );
}

function outcomeHeading(run: Run | null | undefined, historical: boolean) {
  if (!run) return "Recommendation unavailable";
  const remediation = Boolean(run.action_trace?.remediation_action);
  const decisionKind = remediation ? "governed-memory retraction" : "recommendation";
  if (run.status === "rejected") return `Rejected ${decisionKind}`;
  if (run.status === "completed") {
    if (remediation) return "Completed governed-memory retraction";
    return historical ? "Completed recommendation" : "Post-correction recommendation";
  }
  return `${historical ? "Historical" : "Current"} ${decisionKind}`;
}

function Outcome({ run, mode }: { run?: Run | null; mode: "historical" | "current" }) {
  const historical = mode === "historical";
  const actionTrace = run?.action_trace;
  const recommendation = actionTrace?.recommendation;
  const remediationAction = actionTrace?.remediation_action;
  const remediationPreview = actionTrace?.preview;
  const execution = actionTrace?.execution;
  const selection = actionTrace?.selection;
  const latestToolCall = actionTrace?.tool_calls?.at(-1);
  const latestObservation = actionTrace?.observations?.at(-1);
  const readsAvailable = Array.isArray(run?.trace?.reads);
  const reads = run?.trace?.reads || [];
  return (
    <article className={cn("outcome", historical ? "outcome-historical" : "outcome-current")}>
      <header>
        <div>
          <Badge tone={historical ? "historical" : "current"}>
            {historical ? "Historical outcome" : "Current outcome"}
          </Badge>
          <h3>{outcomeHeading(run, historical)}</h3>
        </div>
        <span className="outcome-status">{humanStatus(run?.status)}</span>
      </header>
      <PlanSections run={run} primary={!historical} />
      {recommendation ? (
        <div
          className="action-execution"
          data-execution-status={execution?.status || "awaiting_approval"}
        >
          <span>Recommendation</span>
          <strong>{humanStatus(execution?.status || "awaiting_approval")}</strong>
          <span>
            {selection?.provider || run?.provider || "Unavailable"} /{" "}
            {selection?.model || run?.model || "Unavailable"}
          </span>
          {recommendation.operational_action ? (
            <span>
              Primary action: {humanStatus(recommendation.operational_action.primary_action)}
            </span>
          ) : null}
        </div>
      ) : null}
      {remediationAction ? (
        <div
          className="action-execution"
          data-execution-status={execution?.status || "awaiting_approval"}
        >
          <span>Governed-memory retraction</span>
          <strong>{humanStatus(execution?.status || "awaiting_approval")}</strong>
          <span>{remediationAction.target_excerpt || "Target excerpt unavailable"}</span>
          <span>
            {`${remediationPreview?.effect_count ?? 0} bounded mutation${remediationPreview?.effect_count === 1 ? "" : "s"} · expires ${formatTime(remediationPreview?.expires_at)}`}
          </span>
          <span>{`Preview ${remediationPreview?.fingerprint || "unavailable"}`}</span>
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
          {execution?.operation_id ? (
            <span>{`Operation ${execution.operation_id}: ${humanStatus(execution.operation_status)}`}</span>
          ) : null}
        </div>
      ) : null}
      {latestToolCall || latestObservation ? (
        <div className="action-observation">
          <span>Diagnostic evidence</span>
          <strong>{latestObservation?.query_key || latestToolCall?.query_key || "Unavailable"}</strong>
          <span>
            {latestObservation?.metric?.namespace || "Unavailable"} /{" "}
            {latestObservation?.metric?.name || "Unavailable"} /{" "}
            {typeof latestObservation?.datapoint_count === "number"
              ? `${latestObservation.datapoint_count} datapoints`
              : "Unavailable"}
          </span>
        </div>
      ) : null}
      {reads.length ? (
        <div className="decision-citations" aria-label={`${mode} decision evidence`}>
          <span>Decision evidence</span>
          {reads.map((read) => {
            const dependencyCount = read.outgoing_lineage_edge_ids?.length || 0;
            return (
              <div className="decision-citation" key={read.id}>
                <div>
                  <strong>{read.writer || "Unavailable"}</strong>
                  <span>{read.source_ref || "Unavailable"}</span>
                </div>
                <IdentifierValue value={read.memory_id} label={`${mode} cited memory`} quiet />
                <p>{read.justification || "Unavailable"}</p>
                <span>
                  {read.outgoing_lineage_edge_ids
                    ? `${dependencyCount} downstream lineage ${dependencyCount === 1 ? "edge" : "edges"}`
                    : "Lineage count unavailable"}
                </span>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="empty-inline compact outcome-empty">
          <Fingerprint aria-hidden="true" size={18} />
          <div>
            <strong>{readsAvailable ? "No recorded memory reads" : "Decision reads unavailable"}</strong>
            <p>
              {readsAvailable
                ? "This decision has no cited read in the trace."
                : "The trace did not include decision-read data."}
            </p>
          </div>
        </div>
      )}
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
  const rejected = rejectedRun(scenario, activeRun);
  const corrected = postCorrectionRun(scenario, activeRun);
  return (
    <section className="comparison" aria-labelledby="comparisonHeading">
      <div className="comparison-heading">
        <div>
          <p className="section-kicker">Recorded decision delta</p>
          <h2 id="comparisonHeading">Before vs after.</h2>
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
      title={memory.content || "Unavailable"}
    >
      <span className="memory-state" aria-hidden="true" />
      <span className="memory-body">
        <strong className="memory-content">
          {memory.content || "Unavailable"}
        </strong>
        <span className="memory-meta">
          <span className="memory-status">
            {invalid ? "invalidated" : review ? "review required" : "current"}
          </span>
          <span>{memory.writer || "Unavailable"}</span>
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
          {snapshot ? `${current.length} live · ${invalid} invalid` : "Unavailable"}
        </span>
      </header>
      <div id="memories" className="memory-list" aria-live="polite">
        {memories.length ? (
          memories.map((memory) => <MemoryRow key={memory.id} memory={memory} />)
        ) : (
          <div className="empty-inline">
            <Fingerprint aria-hidden="true" size={22} />
            <div>
              <strong>{snapshot ? "No beliefs in this state" : "Belief state unavailable"}</strong>
              <p>
                {snapshot
                  ? "No durable belief versions were returned for this state."
                  : "The replay did not include a belief snapshot."}
              </p>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

export function InfluenceLedger({
  influence,
  state = "ready",
  error = "",
}: {
  influence: InfluenceItem[];
  state?: "loading" | "ready" | "empty" | "error";
  error?: string;
}) {
  const body = state === "loading" ? (
    <div className="empty-inline compact" role="status" aria-live="polite">
      <Pulse aria-hidden="true" size={20} />
      <div>
        <strong>Loading decision evidence</strong>
        <p>Resolving the recorded reads for this decision.</p>
      </div>
    </div>
  ) : state === "error" ? (
    <div className="empty-inline compact inline-error" role="alert">
      <Warning aria-hidden="true" size={20} />
      <div>
        <strong>Decision evidence unavailable</strong>
        <p>{error || "Unavailable"}</p>
      </div>
    </div>
  ) : influence.length ? (
    influence.map((item, index) => {
      const memory = item.memory;
      const rank = item.read?.rank;
      return (
        <article key={item.read?.id || memory?.id || `influence-${index}`}>
          <div
            className="influence-rank"
            aria-label={typeof rank === "number" ? `Rank ${rank}` : "Rank unavailable"}
          >
            {typeof rank === "number" ? String(rank).padStart(2, "0") : "Unavailable"}
          </div>
          <div>
            <strong>{memory?.content || "Unavailable"}</strong>
            <p>
              {item.provenance?.writer || memory?.writer || "Unavailable"} / rank{" "}
              {typeof rank === "number" ? rank : "Unavailable"}
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
        <strong>No recorded reads</strong>
        <p>This decision did not return a cited memory read.</p>
      </div>
    </div>
  );
  return (
    <section className="influence-pane" aria-labelledby="influenceTitle">
      <header className="pane-heading">
        <div>
          <p className="section-kicker">Decision evidence</p>
          <h2 id="influenceTitle">Cited memory reads</h2>
        </div>
        <span id="influenceCount" className="metric">
          {state === "ready" || state === "empty"
            ? `${influence.length} read${influence.length === 1 ? "" : "s"}`
            : "Unavailable"}
        </span>
      </header>
      <div id="influenceList" className="influence-list">{body}</div>
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
            {snapshot
              ? snapshot.as_of
                ? `As of ${formatTime(snapshot.as_of)}`
                : "Live belief state"
              : "Unavailable"}
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
  const effectSummary = [
    operation.invalidated_memory_ids
      ? `closed ${operation.invalidated_memory_ids.length}`
      : null,
    operation.restored_memory_ids
      ? `restored ${operation.restored_memory_ids.length}`
      : null,
    operation.effects && reviewCount ? `review ${reviewCount}` : null,
  ].filter(Boolean).join(" / ");
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
      <p>{operation.reason || "Unavailable"}</p>
      <span>{effectSummary || "Effect counts unavailable"}</span>
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

function uniqueValues(values: Array<string | null | undefined>) {
  return [...new Set(values.filter((value): value is string => Boolean(value?.trim())))];
}

function reasoningModels(runs: Run[]) {
  return uniqueValues(
    runs.map((item) => {
      const provider = item.provider || item.action_trace?.selection?.provider;
      const model = item.model || item.action_trace?.selection?.model;
      return provider || model ? `${provider || "Unavailable"} / ${model || "Unavailable"}` : null;
    }),
  );
}

function embeddingProfiles(runs: Run[]) {
  const profiles: string[] = [];
  for (const item of runs) {
    const retrievals = item.trace?.retrievals || [];
    for (const read of item.trace?.reads || []) {
      const retrieval = retrievals.find((candidate) => candidate.id === read.retrieval_id);
      const profileId = read.embedding_profile_id || retrieval?.embedding_profile_id;
      const provider = retrieval?.embedding_provider;
      const model = retrieval?.embedding_model;
      if (!profileId && !provider && !model) continue;
      const modelLabel = provider || model
        ? `${provider || "Unavailable"} / ${model || "Unavailable"}`
        : null;
      profiles.push([modelLabel, profileId].filter(Boolean).join(" · "));
    }
  }
  return uniqueValues(profiles);
}

function FactValues({ values }: { values: string[] }) {
  if (!values.length) return <>Unavailable</>;
  return (
    <span className="fact-values">
      {values.map((value) => (
        <span key={value} title={value}>{value}</span>
      ))}
    </span>
  );
}

export function StoryHeader({
  incident,
  namespace,
  run,
  scenario,
}: {
  incident: Incident | null;
  namespace: string;
  run: Run | null;
  scenario: SignatureScenario | null;
}) {
  const runs = [...(scenario?.runs || []), ...(run ? [run] : [])].filter(
    (item, index, items) => items.findIndex((candidate) => candidate.id === item.id) === index,
  );
  const postCorrection = postCorrectionRun(scenario, run);
  const recordedIncident = scenario?.incident || incident;
  const service =
    run?.service_slug || postCorrection?.service_slug || recordedIncident?.service_slug;
  const models = reasoningModels(runs);
  const profiles = embeddingProfiles(runs);
  return (
    <section className="story-header" aria-labelledby="incidentHeading">
      <div className="story-copy">
        <p className="section-kicker">Causal incident replay</p>
        <h1 id="incidentHeading">{recordedIncident?.title || "Incident unavailable"}</h1>
        <p id="incidentSummary">
          {recordedIncident?.summary || "Unavailable"}
        </p>
      </div>
      <dl className="story-facts">
        <div>
          <dt>Severity</dt>
          <dd id="incidentSeverity">{recordedIncident?.severity || "Unavailable"}</dd>
        </div>
        <div>
          <dt>Service</dt>
          <dd id="incidentService">{service || "Unavailable"}</dd>
        </div>
        <div>
          <dt>Replay status</dt>
          <dd>{humanStatus(scenario?.status)}</dd>
        </div>
        <div>
          <dt>Current run</dt>
          <dd id="runStatus">{humanStatus(run?.status)}</dd>
        </div>
        <div className="fact-models">
          <dt>Reasoning provider / model</dt>
          <dd>
            <FactValues values={models} />
          </dd>
        </div>
        <div className="fact-models">
          <dt>Embedding profiles tied to reads</dt>
          <dd>
            <FactValues values={profiles} />
          </dd>
        </div>
        <div className="fact-wide">
          <dt>Namespace</dt>
          <dd id="namespace" title={namespace}>{namespace}</dd>
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
        {Array.from({ length: 4 }, (_, index) => (
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

export function EmptySurface({ onSignIn }: { onSignIn: () => void }) {
  return (
    <section className="state-surface" aria-labelledby="emptyHeading">
      <Fingerprint aria-hidden="true" size={32} />
      <p className="section-kicker">Replay unavailable</p>
      <h1 id="emptyHeading">No incident replay is available.</h1>
      <p>
        Sign in to create a scenario, or return when a recorded incident trace is available.
      </p>
      <Button type="button" variant="primary" onClick={onSignIn}>
        <SignIn aria-hidden="true" size={16} weight="bold" />
        Sign in
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
      <p>{message || "Unavailable"}</p>
      <Button type="button" variant="quiet" onClick={onRetry}>
        <ArrowClockwise aria-hidden="true" size={16} />
        Retry trace
      </Button>
    </section>
  );
}
