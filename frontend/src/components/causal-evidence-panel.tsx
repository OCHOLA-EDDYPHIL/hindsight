import { DownloadSimple } from "@phosphor-icons/react";

import {
  IdentifierValue,
  boundCausalEnvelope,
  evidenceState,
  postCorrectionRun,
  rejectedRun,
} from "@/components/cockpit";
import { humanStatus } from "@/lib/format";
import type {
  CausalEnvelope,
  OperationalAction,
  Run,
  SignatureScenario,
} from "@/types";

import "@/components/causal-evidence-panel.css";

function evidenceStateLabel(state: ReturnType<typeof evidenceState>) {
  return {
    changed: "Controlled action change",
    unchanged: "Controlled action unchanged",
    unavailable: "Evidence unavailable",
    mismatched: "Invariant mismatch",
    "corrected-only": "Corrected result only",
  }[state];
}

function CausalEnvelopeDetails({
  label,
  run,
  envelopeSha256,
  action,
}: {
  label: string;
  run: Run | null;
  envelopeSha256?: string | null;
  action: OperationalAction | null;
}) {
  const trace = run?.action_trace;
  const envelope = boundCausalEnvelope(run, envelopeSha256);
  const recordedAction = envelope ? action : null;
  const observations = envelope?.invariant_inputs?.ordered_observations || [];
  const memoryVersions = orderedMemoryVersions(envelope);

  return (
    <details className="causal-envelope-details" open>
      <summary>{label}</summary>
      <div className="envelope-facts">
        <span>{run?.provider || trace?.selection?.provider || "Provider unavailable"}</span>
        <span>{run?.model || trace?.selection?.model || "Model unavailable"}</span>
        <span>{envelope?.identity.release_revision || "Release unavailable"}</span>
      </div>
      <div className="envelope-action">
        <span>Server-rendered directive</span>
        <strong>{recordedAction?.directive || "Unavailable"}</strong>
        <dl>
          <div>
            <dt>Primary action</dt>
            <dd>{recordedAction?.primary_action || "Unavailable"}</dd>
          </div>
          <div>
            <dt>Semantic fingerprint</dt>
            <dd>{recordedAction?.fingerprint || "Unavailable"}</dd>
          </div>
        </dl>
      </div>
      <div className="envelope-observations">
        {observations.length ? observations.map((observation, observationIndex) => (
          <article key={observation.id || `${label}:${observationIndex}`}>
            <header>
              <strong>{observation.query_key}</strong>
              <span>{observation.region || "Region unavailable"}</span>
            </header>
            <dl>
              <div>
                <dt>Metric</dt>
                <dd>{`${observation.metric?.namespace || "Unavailable"} / ${observation.metric?.name || "Unavailable"}`}</dd>
              </div>
              <div>
                <dt>Statistic / unit</dt>
                <dd>{`${observation.metric?.statistic || "Unavailable"} / ${observation.metric?.unit || "Unavailable"}`}</dd>
              </div>
              <div>
                <dt>Window</dt>
                <dd>{`${observation.window?.start || "Unavailable"} → ${observation.window?.end || "Unavailable"}`}</dd>
              </div>
              <div>
                <dt>Period / truncated</dt>
                <dd>{`${observation.metric?.period_seconds ?? "Unavailable"}s / ${observation.truncated === true ? "yes" : observation.truncated === false ? "no" : "Unavailable"}`}</dd>
              </div>
              <div className="envelope-wide">
                <dt>Dimensions</dt>
                <dd>
                  {(observation.metric?.dimensions || []).map((dimension) => (
                    `${dimension.name}=${dimension.value}`
                  )).join(" · ") || "Unavailable"}
                </dd>
              </div>
            </dl>
            <table>
              <caption>Ordered telemetry datapoints</caption>
              <thead><tr><th>Timestamp</th><th>Value</th></tr></thead>
              <tbody>
                {(observation.datapoints || []).map((point, pointIndex) => (
                  <tr key={`${point.timestamp}:${pointIndex}`}>
                    <td>{point.timestamp}</td>
                    <td>{point.value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="query-fingerprint">
              Query fingerprint: {observation.query_fingerprint || "Unavailable"}
            </p>
          </article>
        )) : <p className="unavailable-value">Complete telemetry unavailable</p>}
      </div>
      <div className="envelope-memories">
        <span>Allowed memory selection</span>
        {memoryVersions.length ? (
          <div className="evidence-table-wrap">
            <table aria-label={`${label} allowed memory versions`}>
              <caption>Exact governed-memory inputs</caption>
              <thead>
                <tr>
                  <th>Order</th><th>Memory</th><th>Belief / version</th><th>Memory SHA-256</th><th>Prompt fragment SHA-256</th>
                </tr>
              </thead>
              <tbody>
                {memoryVersions.map((item, index) => (
                  <tr
                    key={item.memory?.memory_id || item.memory_sha256 || `${label}:${index}`}
                    data-memory-id={item.memory?.memory_id}
                  >
                    <td>{item.ordinal ?? index + 1}</td>
                    <td>{item.memory?.memory_id || "Unavailable"}</td>
                    <td>{`${item.memory?.belief_id || "Unavailable"} / v${item.memory?.version ?? "Unavailable"}`}</td>
                    <td>{item.memory_sha256 || "Unavailable"}</td>
                    <td>{item.prompt_fragment_sha256 || "Unavailable"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <p className="unavailable-value">Allowed memory selection unavailable</p>}
      </div>
      <dl className="envelope-identities">
        <div>
          <dt>Observation fingerprint</dt>
          <dd>{trace?.observation_fingerprint || "Unavailable"}</dd>
        </div>
        <div>
          <dt>Invariant envelope</dt>
          <dd>{envelope?.invariant_inputs_sha256 || "Unavailable"}</dd>
        </div>
        <div>
          <dt>Rendered prompt</dt>
          <dd>{envelope?.rendered_prompt_sha256?.join(" · ") || "Unavailable"}</dd>
        </div>
        <div>
          <dt>Selection fingerprint</dt>
          <dd>{envelope?.permitted_intervention.selection_fingerprint || "Unavailable"}</dd>
        </div>
        <div>
          <dt>Envelope SHA-256</dt>
          <dd>{envelope?.envelope_sha256 || "Unavailable"}</dd>
        </div>
      </dl>
    </details>
  );
}

function orderedMemoryVersions(envelope: CausalEnvelope | null) {
  const versions = envelope?.permitted_intervention.ordered_memory_versions || [];
  return [
    ...new Map(
      versions.map((item, index) => [
        item.memory?.memory_id || item.memory_sha256 || `memory:${index}`,
        item,
      ]),
    ).values(),
  ];
}

function MemoryDeltaEvidence({
  before,
  after,
}: {
  before: CausalEnvelope | null;
  after: CausalEnvelope | null;
}) {
  const beforeVersions = orderedMemoryVersions(before);
  const afterVersions = orderedMemoryVersions(after);
  const beforeById = new Map(
    beforeVersions.flatMap((item) => item.memory?.memory_id ? [[item.memory.memory_id, item] as const] : []),
  );
  const afterById = new Map(
    afterVersions.flatMap((item) => item.memory?.memory_id ? [[item.memory.memory_id, item] as const] : []),
  );
  const memoryIds = [...new Set([...beforeById.keys(), ...afterById.keys()])];

  return (
    <details className="memory-delta-evidence" open>
      <summary>Allowed memory delta</summary>
      {before && after && memoryIds.length ? (
        <div className="evidence-table-wrap">
          <table aria-label="Allowed memory delta">
            <thead><tr><th>Delta</th><th>Memory</th><th>Before SHA-256</th><th>After SHA-256</th></tr></thead>
            <tbody>
              {memoryIds.map((memoryId) => {
                const beforeItem = beforeById.get(memoryId);
                const afterItem = afterById.get(memoryId);
                const delta = beforeItem && afterItem ? "retained" : beforeItem ? "removed" : "added";
                return (
                  <tr key={memoryId} data-delta-status={delta} data-memory-id={memoryId}>
                    <td>{delta}</td>
                    <td>{memoryId}</td>
                    <td>{beforeItem?.memory_sha256 || "Unavailable"}</td>
                    <td>{afterItem?.memory_sha256 || "Unavailable"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : <p className="unavailable-value">Allowed memory delta unavailable</p>}
    </details>
  );
}

function CorrectionEvidence({
  scenario,
  envelope,
}: {
  scenario: SignatureScenario | null;
  envelope: CausalEnvelope | null;
}) {
  const intervention = envelope?.permitted_intervention;
  const operation = scenario?.operation;
  const operationBound = Boolean(
    operation && intervention?.correction_operation_id === operation.id,
  );
  const effects = operationBound ? intervention?.operation_effects || [] : [];
  const invalidated = operationBound ? operation?.invalidated_memory_ids || [] : [];
  const restored = operationBound ? operation?.restored_memory_ids || [] : [];
  const invalidatedFingerprints = intervention?.invalidated_memory_fingerprints || [];
  const restoredFingerprints = intervention?.restored_memory_fingerprints || [];
  const memoryEffects = [
    ...invalidated.map((memoryId, index) => ({
      disposition: "invalidated",
      memoryId,
      fingerprint: invalidatedFingerprints[index],
    })),
    ...restored.map((memoryId, index) => ({
      disposition: "restored",
      memoryId,
      fingerprint: restoredFingerprints[index],
    })),
  ];

  return (
    <section className="declared-intervention" aria-labelledby="declaredInterventionHeading">
      <span id="declaredInterventionHeading">Declared memory intervention</span>
      {operationBound ? (
        <>
          <dl className="correction-facts">
            <div><dt>Operation</dt><dd>{operation?.id}</dd></div>
            <div><dt>Target timestamp</dt><dd>{intervention?.correction_target_timestamp || "Unavailable"}</dd></div>
          </dl>
          <div className="evidence-table-wrap">
            <table aria-label="Correction memory versions">
              <caption>Invalidation and restoration binding</caption>
              <thead><tr><th>Disposition</th><th>Memory</th><th>Memory ID fingerprint</th></tr></thead>
              <tbody>
                {memoryEffects.map((item) => (
                  <tr key={`${item.disposition}:${item.memoryId}`} data-memory-id={item.memoryId}>
                    <td>{item.disposition}</td><td>{item.memoryId}</td><td>{item.fingerprint || "Unavailable"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="evidence-table-wrap">
            <table aria-label="Correction operation effects">
              <caption>Ordered operation effects</caption>
              <thead><tr><th>Sequence</th><th>Effect</th><th>Source</th><th>Result</th><th>Belief</th><th>Namespace</th></tr></thead>
              <tbody>
                {effects.map((effect, index) => (
                  <tr key={`${effect.sequence ?? index + 1}:${effect.effect_type}`}>
                    <td>{effect.sequence ?? index + 1}</td>
                    <td>{effect.effect_type}</td>
                    <td>{effect.source_memory_id || "Unavailable"}</td>
                    <td>{effect.result_memory_id || "Unavailable"}</td>
                    <td>{effect.belief_id || "Unavailable"}</td>
                    <td>{effect.namespace || "Unavailable"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : <p className="unavailable-value">Correction binding unavailable</p>}
    </section>
  );
}

function PostCorrectionExclusion({
  scenario,
  beforeRun,
  afterRun,
  beforeEnvelope,
  afterEnvelope,
}: {
  scenario: SignatureScenario | null;
  beforeRun: Run | null;
  afterRun: Run | null;
  beforeEnvelope: CausalEnvelope | null;
  afterEnvelope: CausalEnvelope | null;
}) {
  const compromisedId = scenario?.stages.compromised_memory_id || scenario?.stages.poison_memory_id;
  const beforeSelected = orderedMemoryVersions(beforeEnvelope).flatMap((item) =>
    item.memory?.memory_id ? [item.memory.memory_id] : [],
  );
  const afterSelected = orderedMemoryVersions(afterEnvelope).flatMap((item) =>
    item.memory?.memory_id ? [item.memory.memory_id] : [],
  );
  const beforeReads = beforeRun?.trace?.reads?.map((read) => read.memory_id) || [];
  const afterReads = afterRun?.trace?.reads?.map((read) => read.memory_id) || [];
  const invalidated = scenario?.operation?.invalidated_memory_ids || [];
  const proof = scenario?.causal_evidence?.proof_states.memory_correction_proven;
  const complete = Boolean(compromisedId && beforeEnvelope && afterEnvelope && scenario?.operation);
  const checks = compromisedId ? [
    beforeSelected.includes(compromisedId),
    beforeReads.includes(compromisedId),
    invalidated.includes(compromisedId),
    !afterSelected.includes(compromisedId),
    !afterReads.includes(compromisedId),
  ] : [];
  const status = !complete || proof?.status === "unavailable" || !proof
    ? "unavailable"
    : proof.status === "proven" && checks.every(Boolean)
      ? "proven"
      : "not_proven";
  const rows = [
    ["Before selection", beforeSelected.join(" · ") || "Unavailable", Boolean(compromisedId && beforeSelected.includes(compromisedId))],
    ["Before cited reads", beforeReads.join(" · ") || "Unavailable", Boolean(compromisedId && beforeReads.includes(compromisedId))],
    ["Operation invalidated", invalidated.join(" · ") || "Unavailable", Boolean(compromisedId && invalidated.includes(compromisedId))],
    ["After selection", afterSelected.join(" · ") || "None", Boolean(compromisedId && !afterSelected.includes(compromisedId))],
    ["After cited reads", afterReads.join(" · ") || "None", Boolean(compromisedId && !afterReads.includes(compromisedId))],
  ] as const;

  return (
    <section className="post-correction-exclusion" data-proof-status={status} aria-labelledby="postCorrectionExclusionHeading">
      <header>
        <div>
          <span>Post-correction exclusion</span>
          <strong id="postCorrectionExclusionHeading">{compromisedId || "Compromised memory unavailable"}</strong>
        </div>
        <b>{humanStatus(status)}</b>
      </header>
      <small>{proof?.reason || "post_correction_exclusion_evidence_unavailable"}</small>
      <div className="evidence-table-wrap">
        <table aria-label="Post-correction exclusion checks">
          <thead><tr><th>Check</th><th>Recorded memory IDs</th><th>Result</th></tr></thead>
          <tbody>
            {rows.map(([label, value, matched]) => (
              <tr key={label}><td>{label}</td><td>{value}</td><td>{complete ? matched ? "verified" : "not verified" : "unavailable"}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function CausalEvidencePanel({
  scenario,
  onDownload,
}: {
  scenario: SignatureScenario | null;
  onDownload: () => void;
}) {
  const evidence = scenario?.causal_evidence;
  const before = rejectedRun(scenario);
  const after = postCorrectionRun(scenario);
  const beforeEnvelope = boundCausalEnvelope(before, evidence?.before_envelope_sha256);
  const afterEnvelope = boundCausalEnvelope(after, evidence?.after_envelope_sha256);
  const state = evidenceState(scenario);
  const states = [
    ["Memory correction", evidence?.proof_states.memory_correction_proven],
    ["Recommendation action delta", evidence?.proof_states.action_delta_proven],
    ["Controlled-pair eligibility", evidence?.proof_states.controlled_pair_eligible],
    ["Repeatability", evidence?.proof_states.repeatable_causal_effect_supported],
    ["Service recovery", evidence?.proof_states.service_recovery_proven],
  ] as const;

  return (
    <section
      className="causal-evidence"
      data-evidence-state={state}
      aria-labelledby="causalEvidenceHeading"
    >
      <div className="rail-heading">
        <div>
          <p className="section-kicker">Conservative proof boundary</p>
          <h2 id="causalEvidenceHeading">Recommendation evidence</h2>
        </div>
        <div className="evidence-heading-actions">
          <strong className="evidence-state">{evidenceStateLabel(state)}</strong>
          {evidence?.download ? (
            <button
              type="button"
              className="evidence-download"
              onClick={onDownload}
            >
              <DownloadSimple aria-hidden="true" size={16} weight="bold" />
              Download JSON
            </button>
          ) : null}
        </div>
      </div>
      <p className="evidence-boundary">
        This records recommendation behavior only. Repeatability and service recovery require
        separate measurements.
      </p>
      <ul aria-label="Causal evidence proof states">
        {states.map(([label, state]) => (
          <li key={label} data-proof-status={state?.status || "unavailable"}>
            <span>{label}</span>
            <strong>{humanStatus(state?.status || "unavailable")}</strong>
            <small>{humanStatus(state?.reason || "evidence_not_available")}</small>
          </li>
        ))}
      </ul>
      <details className="invariant-comparison" open>
        <summary>Invariant comparison matrix</summary>
        {evidence?.controlled_pair_checks?.length ? (
          <table>
            <thead>
              <tr><th>Contract field</th><th>Check</th><th>Reason code</th></tr>
            </thead>
            <tbody>
              {evidence.controlled_pair_checks.map((check) => (
                <tr key={check.field} data-check-status={check.status}>
                  <td>{check.field}</td>
                  <td>{humanStatus(check.status)}</td>
                  <td>{check.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="unavailable-value">Comparison matrix unavailable</p>}
      </details>
      <div className="causal-envelope-grid">
        <CausalEnvelopeDetails
          label="Before correction"
          run={before || null}
          envelopeSha256={evidence?.before_envelope_sha256}
          action={scenario?.action_comparison?.before || null}
        />
        <CausalEnvelopeDetails
          label="After correction"
          run={after || null}
          envelopeSha256={evidence?.after_envelope_sha256}
          action={scenario?.action_comparison?.after || null}
        />
      </div>
      <MemoryDeltaEvidence before={beforeEnvelope} after={afterEnvelope} />
      <CorrectionEvidence scenario={scenario} envelope={afterEnvelope} />
      <PostCorrectionExclusion
        scenario={scenario}
        beforeRun={before || null}
        afterRun={after || null}
        beforeEnvelope={beforeEnvelope}
        afterEnvelope={afterEnvelope}
      />
      <div className="evidence-digest">
        <span>Download digest</span>
        <IdentifierValue
          value={evidence?.download?.sha256}
          label="causal evidence digest"
          quiet
        />
      </div>
    </section>
  );
}
