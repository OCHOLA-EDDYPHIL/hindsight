import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  BeliefLedger,
  CausalRail,
  ErrorSurface,
  InfluenceLedger,
  LoadingSurface,
  OperationLedger,
  OutcomeComparison,
  StoryHeader,
  Timeline,
} from "@/components/cockpit";
import { CausalEvidencePanel } from "@/components/causal-evidence-panel";
import type {
  CausalEnvelope,
  DiagnosticObservation,
  OperationEffect,
  Run,
  SignatureScenario,
  Snapshot,
} from "@/types";

const BEFORE_ENVELOPE_SHA = `sha256:${"1".repeat(64)}`;
const AFTER_ENVELOPE_SHA = `sha256:${"2".repeat(64)}`;
const COMPROMISED_MEMORY_SHA = `sha256:${"3".repeat(64)}`;
const RESTORED_MEMORY_SHA = `sha256:${"4".repeat(64)}`;
const COMPROMISED_FRAGMENT_SHA = `sha256:${"5".repeat(64)}`;
const RESTORED_FRAGMENT_SHA = `sha256:${"6".repeat(64)}`;
const BEFORE_SELECTION_FINGERPRINT = `selection:${"9".repeat(64)}`;
const AFTER_SELECTION_FINGERPRINT = `selection:${"8".repeat(64)}`;

const controlledObservation: DiagnosticObservation = {
  id: "observation-controlled",
  tool_call_id: "diagnostic-controlled",
  schema_version: 1,
  tool: "aws_cloudwatch_diagnostics",
  query_key: "payments.retry_fanout",
  query_fingerprint: `cloudwatch_query:${"b".repeat(64)}`,
  region: "us-east-1",
  metric: {
    namespace: "Hindsight/ControlledIncidentTelemetry",
    name: "RetryFanout",
    dimensions: [{ name: "Service", value: "payments-api" }],
    statistic: "Maximum",
    unit: "Count",
    period_seconds: 60,
  },
  window: {
    start: "2026-07-17T10:15:00Z",
    end: "2026-07-17T10:30:00Z",
    seconds: 900,
  },
  datapoints: [
    { timestamp: "2026-07-17T10:28:00Z", value: 7 },
    { timestamp: "2026-07-17T10:29:00Z", value: 8 },
  ],
  datapoint_count: 2,
  truncated: false,
};

const correctionEffects: OperationEffect[] = [
  {
    sequence: 1,
    effect_type: "closed",
    source_memory_id: "memory-compromised",
    result_memory_id: null,
    belief_id: "belief-payments-guidance",
    namespace: "demo:payments-poison-rewind:session:49109a44",
  },
  {
    sequence: 2,
    effect_type: "reasserted",
    source_memory_id: "memory-baseline",
    result_memory_id: "memory-restored",
    belief_id: "belief-payments-guidance",
    namespace: "demo:payments-poison-rewind:session:49109a44",
  },
];

function controlledEnvelope(afterCorrection: boolean): CausalEnvelope {
  const memoryId = afterCorrection ? "memory-restored" : "memory-compromised";
  const memorySha = afterCorrection ? RESTORED_MEMORY_SHA : COMPROMISED_MEMORY_SHA;
  const fragmentSha = afterCorrection ? RESTORED_FRAGMENT_SHA : COMPROMISED_FRAGMENT_SHA;
  return {
    schema_version: 4,
    canonicalization: "hindsight.canonical-json.v1",
    identity: {
      scenario_id: "49109a44-43e7-40de-b547-b4f9d0a387a2",
      namespace: "demo:payments-poison-rewind:session:49109a44",
      replay_anchor: "2026-07-17T10:30:00Z",
      scenario_routing_key: "signature:controlled",
      release_revision: "a".repeat(40),
    },
    invariant_inputs: {
      ordered_observations: [structuredClone(controlledObservation)],
      release_revision: "a".repeat(40),
    },
    invariant_inputs_sha256: `sha256:${"7".repeat(64)}`,
    permitted_intervention: {
      kind: "governed_memory_version_selection.v1",
      ordered_memory_versions: [
        {
          ordinal: 1,
          memory: {
            memory_id: memoryId,
            belief_id: "belief-payments-guidance",
            version: afterCorrection ? 3 : 2,
          },
          memory_sha256: memorySha,
          prompt_fragment_sha256: fragmentSha,
        },
      ],
      selection_fingerprint: afterCorrection
        ? AFTER_SELECTION_FINGERPRINT
        : BEFORE_SELECTION_FINGERPRINT,
      expected_changed_prompt_fragments: [fragmentSha],
      correction_operation_id: afterCorrection ? "operation-rewind" : null,
      correction_target_timestamp: afterCorrection ? "2026-07-17T10:30:00Z" : null,
      operation_effects: afterCorrection ? structuredClone(correctionEffects) : [],
      invalidated_memory_fingerprints: afterCorrection
        ? [`sha256:${"c".repeat(64)}`]
        : [],
      restored_memory_fingerprints: afterCorrection
        ? [`sha256:${"d".repeat(64)}`]
        : [],
    },
    rendered_prompt_sha256: [afterCorrection ? RESTORED_FRAGMENT_SHA : COMPROMISED_FRAGMENT_SHA],
    envelope_sha256: afterCorrection ? AFTER_ENVELOPE_SHA : BEFORE_ENVELOPE_SHA,
  };
}

const scenario: SignatureScenario = {
  scenario_id: "49109a44-43e7-40de-b547-b4f9d0a387a2",
  namespace: "demo:payments-poison-rewind:session:49109a44",
  status: "completed",
  session_status: "active",
  rewind_anchor: "2026-07-17T10:30:00Z",
  completed_at: "2026-07-17T11:30:00Z",
  incident: {
    slug: "demo-payments-checkout-latency:49109a44",
    title: "Checkout latency under retry amplification",
    summary: "A payment processor timeout multiplied checkout retries.",
    severity: "SEV-1",
    service_slug: "payments-api",
  },
  runs: [
    {
      id: "run-rejected",
      status: "rejected",
      service_slug: "payments-api",
      decision_id: "decision-rejected",
      action_approved: false,
      plan: "Scale payment workers while downstream retry fanout remains elevated.",
      proposed_action: "Scale payment workers while retry fanout remains elevated.",
      trace: {
        retrievals: [
          {
            id: "retrieval-rejected",
            embedding_profile_id: "profile-gemini-001",
            embedding_provider: "gemini",
            embedding_model: "text-embedding-004",
          },
        ],
        reads: [
          {
            id: "read-compromised",
            memory_id: "memory-compromised",
            belief_id: "belief-payments-guidance",
            version_number: 2,
            retrieval_id: "retrieval-rejected",
            embedding_profile_id: "profile-gemini-001",
            writer: "demo.fixture-import",
            source_ref: "demo:stale-runbook-import",
            justification: "Previously approved retry guidance is stale for this incident.",
            outgoing_lineage_edge_ids: ["edge-compromised-reflection"],
          },
        ],
      },
      action_trace: {
        schema_version: 4,
        mode: "recommendation_only",
        selection: {
          fingerprint: "b".repeat(64),
          provider: "gemini",
          model: "gemini-2.5-flash",
        },
        recommendation: {
          id: `recommendation:${"a".repeat(64)}`,
          summary: "Scale payment workers while retry fanout remains elevated.",
          status: "awaiting_approval",
          operational_action: {
            catalog_id: "payments_retry_amplification.actions.v1",
            contract: "payments_retry_amplification.v1",
            action_id: "scale_workers",
            disposition: "recommend",
            parameters: {},
            primary_action: "scale_workers",
            directive: "Scale payment workers.",
            consistency_status: "consistent",
            fingerprint: "operational_action:before",
          },
        },
        execution: { status: "not_executed", mode: "recommendation_only" },
        observation_fingerprint: `telemetry:${"e".repeat(64)}`,
        causal_envelope: controlledEnvelope(false),
        tool_calls: [
          {
            id: "diagnostic-controlled",
            tool: "aws_cloudwatch_diagnostics",
            query_key: "payments.retry_fanout",
            status: "completed",
          },
        ],
        observations: [structuredClone(controlledObservation)],
      },
    },
    {
      id: "run-corrected",
      status: "completed",
      service_slug: "payments-api",
      decision_id: "decision-corrected",
      action_approved: true,
      plan: "Retry fanout amplified processor timeouts; inspect queue depth; throttle retry workers.",
      proposed_action: "Throttle retry fanout while processor health recovers.",
      trace: {
        retrievals: [
          {
            id: "retrieval-corrected",
            embedding_profile_id: "profile-gemini-002",
            embedding_provider: "gemini",
            embedding_model: "text-embedding-004",
          },
        ],
        reads: [
          {
            id: "read-baseline",
            memory_id: "memory-restored",
            belief_id: "belief-payments-guidance",
            version_number: 3,
            retrieval_id: "retrieval-corrected",
            embedding_profile_id: "profile-gemini-002",
            writer: "demo.seed",
            source_ref: "demo:known-good-payment-incident",
            justification: "Resolved incident evidence supports throttling retries.",
            outgoing_lineage_edge_ids: ["edge-baseline-reflection"],
          },
        ],
      },
      action_trace: {
        schema_version: 4,
        mode: "recommendation_only",
        selection: {
          fingerprint: "d".repeat(64),
          provider: "gemini",
          model: "gemini-2.5-flash",
        },
        recommendation: {
          id: `recommendation:${"c".repeat(64)}`,
          summary: "Throttle retry fanout while processor health recovers.",
          status: "awaiting_approval",
          operational_action: {
            catalog_id: "payments_retry_amplification.actions.v1",
            contract: "payments_retry_amplification.v1",
            action_id: "throttle_retries",
            disposition: "recommend",
            parameters: {},
            primary_action: "throttle_retries",
            directive: "Throttle retry fanout.",
            consistency_status: "consistent",
            fingerprint: "operational_action:after",
          },
        },
        execution: { status: "recommendation_approved", mode: "recommendation_only" },
        observation_fingerprint: `telemetry:${"e".repeat(64)}`,
        causal_envelope: controlledEnvelope(true),
        tool_calls: [
          {
            id: "diagnostic-controlled",
            tool: "aws_cloudwatch_diagnostics",
            query_key: "payments.retry_fanout",
            status: "completed",
          },
        ],
        observations: [structuredClone(controlledObservation)],
      },
    },
  ],
  operation: {
    id: "operation-rewind",
    operation_type: "rewind",
    status: "completed",
    target_timestamp: "2026-07-17T10:30:00Z",
    invalidated_memory_ids: ["memory-compromised"],
    restored_memory_ids: ["memory-restored"],
    effects: structuredClone(correctionEffects),
  },
  operation_effects: structuredClone(correctionEffects),
  memories: [],
  action_comparison: {
    status: "changed",
    contract: "payments_retry_amplification.v1",
    before: {
      decision_id: "decision-rejected",
      catalog_id: "payments_retry_amplification.actions.v1",
      contract: "payments_retry_amplification.v1",
      action_id: "scale_workers",
      disposition: "recommend",
      parameters: {},
      primary_action: "scale_workers",
      directive: "Scale payment workers.",
      consistency_status: "consistent",
      fingerprint: "operational_action:before",
    },
    after: {
      decision_id: "decision-corrected",
      catalog_id: "payments_retry_amplification.actions.v1",
      contract: "payments_retry_amplification.v1",
      action_id: "throttle_retries",
      disposition: "recommend",
      parameters: {},
      primary_action: "throttle_retries",
      directive: "Throttle retry fanout.",
      consistency_status: "consistent",
      fingerprint: "operational_action:after",
    },
    context: { prompt_equal: true, normalized_telemetry_equal: true },
    memory_correction_proven: true,
    controlled_pair: true,
  },
  causal_evidence: {
    schema_version: 1,
    canonicalization: "hindsight.canonical-json.v1",
    scope: "recommendation_only",
    proof_states: {
      memory_correction_proven: {
        status: "proven",
        reason: "rewind_lineage_and_reads_verified",
      },
      action_delta_proven: { status: "proven", reason: "catalog_action_changed" },
      controlled_pair_eligible: {
        status: "proven",
        reason: "fixed_context_and_memory_delta_verified",
      },
      repeatable_causal_effect_supported: {
        status: "unavailable",
        reason: "repeated_trials_not_measured",
      },
      service_recovery_proven: {
        status: "unavailable",
        reason: "service_recovery_not_measured",
      },
    },
    controlled_pair_checks: [
      {
        field: "invariant_inputs.ordered_observations",
        status: "matched",
        reason: "invariant_inputs_ordered_observations_matched",
      },
      {
        field: "permitted_intervention.ordered_memory_versions",
        status: "matched",
        reason: "declared_memory_intervention_changed",
      },
    ],
    before_envelope_sha256: BEFORE_ENVELOPE_SHA,
    after_envelope_sha256: AFTER_ENVELOPE_SHA,
    download: {
      url: "/v1/signature-scenarios/49109a44-43e7-40de-b547-b4f9d0a387a2/evidence",
      protected_url: "/v2/signature-scenarios/49109a44-43e7-40de-b547-b4f9d0a387a2/evidence",
      sha256: `sha256:${"a".repeat(64)}`,
      media_type: "application/json",
    },
  },
  stages: {
    baseline_memory_id: "memory-baseline",
    compromised_memory_id: "memory-compromised",
    influenced_decision_id: "decision-rejected",
    rewind_operation_id: "operation-rewind",
    corrected_decision_id: "decision-corrected",
  },
};

const snapshot: Snapshot = {
  mode: "current",
  namespace: scenario.namespace,
  as_of: null,
  timeline: ["2026-07-17T10:00:00Z", "2026-07-17T11:00:00Z"],
  memories: [
    {
      id: "memory-baseline",
      content: "Throttle retry fanout when processor timeouts rise.",
      writer: "demo.seed",
      trust_status: "active",
      status: "current",
    },
    {
      id: "memory-compromised",
      content: "Stale guidance recommends scaling workers into retry pressure.",
      writer: "demo.fixture-import",
      status: "invalidated",
      t_invalid: "2026-07-17T11:00:00Z",
    },
  ],
  operations: [
    {
      id: "operation-rewind",
      operation_type: "rewind",
      status: "completed",
      reason: "Remove stale guidance",
      invalidated_memory_ids: ["memory-compromised"],
      restored_memory_ids: [],
    },
  ],
};

const remediationActionTrace: NonNullable<Run["action_trace"]> = {
  schema_version: 3,
  mode: "governed_memory_remediation",
  selection: { fingerprint: "b".repeat(64) },
  remediation_action: {
    id: `remediation_action:${"a".repeat(64)}`,
    name: "retract_recalled_memory",
    target_memory_id: "memory-unsafe",
    target_excerpt: "Increase retry fanout during saturation.",
  },
  preview: {
    id: "preview-1",
    fingerprint: "d".repeat(64),
    expires_at: "2026-08-10T23:15:00Z",
    effect_count: 2,
    effects: {
      close_memory_ids: ["memory-unsafe"],
      review_resolutions: [
        {
          id: "review-unsafe",
          semantic_memory_id: "memory-unsafe",
          status: "superseded",
        },
      ],
    },
  },
  execution: {
    status: "completed",
    mode: "governed_memory_remediation",
    operation_id: "operation-1",
    operation_status: "completed",
  },
};

describe("guided replay cockpit", () => {
  it("renders digest-bound changed evidence with exact memory, operation, and exclusion proof", () => {
    const onDownload = vi.fn();
    const { container } = render(
      <CausalEvidencePanel scenario={scenario} onDownload={onDownload} />,
    );

    expect(container.querySelector(".causal-evidence")).toHaveAttribute(
      "data-evidence-state",
      "changed",
    );
    expect(screen.getByText("Controlled action change")).toBeVisible();
    const states = screen.getByRole("list", { name: "Causal evidence proof states" });
    expect(within(states).getByText("Memory correction")).toBeVisible();
    expect(within(states).getByText("Recommendation action delta")).toBeVisible();
    expect(within(states).getByText("Repeatability")).toBeVisible();
    expect(within(states).getByText("Service recovery")).toBeVisible();
    expect(within(states).getAllByText("unavailable")).toHaveLength(2);
    expect(screen.getByText(/recommendation behavior only/i)).toBeVisible();
    expect(screen.getByText("Invariant comparison matrix")).toBeVisible();
    expect(screen.getByText("invariant_inputs.ordered_observations")).toBeVisible();
    expect(screen.getAllByText("2026-07-17T10:29:00Z")).toHaveLength(2);
    expect(screen.getAllByText("Service=payments-api")).toHaveLength(2);
    expect(screen.getAllByText("8")).toHaveLength(2);

    const beforeMemories = screen.getByRole("table", {
      name: "Before correction allowed memory versions",
    });
    expect(within(beforeMemories).getByText("memory-compromised")).toBeVisible();
    expect(within(beforeMemories).getByText("belief-payments-guidance / v2")).toBeVisible();
    expect(within(beforeMemories).getByText(COMPROMISED_MEMORY_SHA)).toBeVisible();
    expect(within(beforeMemories).getByText(COMPROMISED_FRAGMENT_SHA)).toBeVisible();
    expect(screen.getByText(BEFORE_SELECTION_FINGERPRINT)).toBeVisible();
    const afterMemories = screen.getByRole("table", {
      name: "After correction allowed memory versions",
    });
    expect(within(afterMemories).getByText("memory-restored")).toBeVisible();
    expect(within(afterMemories).getByText("belief-payments-guidance / v3")).toBeVisible();
    expect(within(afterMemories).getByText(RESTORED_MEMORY_SHA)).toBeVisible();
    expect(screen.getByText(AFTER_SELECTION_FINGERPRINT)).toBeVisible();

    const delta = screen.getByRole("table", { name: "Allowed memory delta" });
    expect(within(delta).getByText("removed")).toBeVisible();
    expect(within(delta).getByText("added")).toBeVisible();
    const correction = screen.getByRole("region", { name: "Declared memory intervention" });
    expect(correction).toHaveTextContent("operation-rewind");
    expect(correction).toHaveTextContent("2026-07-17T10:30:00Z");
    expect(correction).toHaveTextContent("memory-compromised");
    expect(correction).toHaveTextContent("memory-restored");
    const effects = screen.getByRole("table", { name: "Correction operation effects" });
    expect(effects).toHaveTextContent("reasserted");
    expect(effects).toHaveTextContent("demo:payments-poison-rewind:session:49109a44");
    const exclusion = screen.getByRole("region", { name: "memory-compromised" });
    expect(exclusion).toHaveAttribute("data-proof-status", "proven");
    expect(exclusion).toHaveTextContent("After selection");
    expect(exclusion).toHaveTextContent("verified");
    fireEvent.click(screen.getByRole("button", { name: "Download JSON" }));
    expect(onDownload).toHaveBeenCalledOnce();
  });

  it("renders an unchanged controlled comparison without claiming an action delta", () => {
    const unchanged = structuredClone(scenario);
    unchanged.action_comparison!.status = "unchanged";
    unchanged.action_comparison!.after = {
      ...unchanged.action_comparison!.before!,
      decision_id: "decision-corrected",
    };
    unchanged.action_comparison!.controlled_pair = false;
    unchanged.causal_evidence!.proof_states.action_delta_proven = {
      status: "not_proven",
      reason: "catalog_action_unchanged",
    };
    const { container } = render(
      <CausalEvidencePanel scenario={unchanged} onDownload={vi.fn()} />,
    );

    expect(container.querySelector(".causal-evidence")).toHaveAttribute(
      "data-evidence-state",
      "unchanged",
    );
    expect(screen.getByText("Controlled action unchanged")).toBeVisible();
    expect(screen.getByText("catalog action unchanged")).toBeVisible();
  });

  it("renders unavailable when a receipt digest does not bind an envelope", () => {
    const unavailable = structuredClone(scenario);
    delete unavailable.runs[1].action_trace!.causal_envelope;
    unavailable.action_comparison!.status = "unavailable";
    unavailable.action_comparison!.contract = null;
    unavailable.action_comparison!.controlled_pair = false;
    unavailable.causal_evidence!.proof_states.controlled_pair_eligible = {
      status: "unavailable",
      reason: "causal_envelope_incomplete_or_invalid",
    };
    unavailable.causal_evidence!.controlled_pair_checks = [{
      field: "causal_envelope",
      status: "unavailable",
      reason: "causal_envelope_incomplete_or_invalid",
    }];
    const { container } = render(
      <CausalEvidencePanel scenario={unavailable} onDownload={vi.fn()} />,
    );

    expect(container.querySelector(".causal-evidence")).toHaveAttribute(
      "data-evidence-state",
      "unavailable",
    );
    expect(screen.getByText("Evidence unavailable")).toBeVisible();
    expect(screen.getAllByText("Complete telemetry unavailable")).toHaveLength(1);
    expect(screen.getByText("Correction binding unavailable")).toBeVisible();
    expect(screen.getByRole("region", { name: "memory-compromised" })).toHaveAttribute(
      "data-proof-status",
      "unavailable",
    );
  });

  it("renders mismatched when any invariant differs even if an action flag says controlled", () => {
    const mismatched = structuredClone(scenario);
    mismatched.causal_evidence!.proof_states.controlled_pair_eligible = {
      status: "not_proven",
      reason: "invariant_inputs_ordered_observations_mismatch",
    };
    mismatched.causal_evidence!.controlled_pair_checks = [{
      field: "invariant_inputs.ordered_observations",
      status: "mismatched",
      reason: "invariant_inputs_ordered_observations_mismatch",
    }];
    mismatched.action_comparison!.controlled_pair = true;
    const { container, rerender } = render(
      <CausalEvidencePanel scenario={mismatched} onDownload={vi.fn()} />,
    );

    expect(container.querySelector(".causal-evidence")).toHaveAttribute(
      "data-evidence-state",
      "mismatched",
    );
    expect(screen.getByText("Invariant mismatch")).toBeVisible();
    expect(screen.queryByText("Controlled action change")).not.toBeInTheDocument();
    rerender(<CausalRail scenario={mismatched} snapshot={snapshot} activeRun={null} />);
    expect(screen.getByRole("heading", {
      name: "Action comparison withheld; invariants differ.",
    })).toBeVisible();
  });

  it("renders a corrected-only result without inventing a before comparison", () => {
    const correctedOnly = structuredClone(scenario);
    correctedOnly.runs = [correctedOnly.runs[1]];
    correctedOnly.stages.influenced_decision_id = null;
    correctedOnly.action_comparison!.status = "unavailable";
    correctedOnly.action_comparison!.contract = null;
    correctedOnly.action_comparison!.before = null;
    correctedOnly.action_comparison!.controlled_pair = false;
    correctedOnly.causal_evidence!.before_envelope_sha256 = null;
    correctedOnly.causal_evidence!.proof_states.controlled_pair_eligible = {
      status: "unavailable",
      reason: "causal_envelope_incomplete_or_invalid",
    };
    correctedOnly.causal_evidence!.controlled_pair_checks = [{
      field: "causal_envelope",
      status: "unavailable",
      reason: "causal_envelope_incomplete_or_invalid",
    }];
    const { container } = render(
      <CausalEvidencePanel scenario={correctedOnly} onDownload={vi.fn()} />,
    );

    expect(container.querySelector(".causal-evidence")).toHaveAttribute(
      "data-evidence-state",
      "corrected-only",
    );
    expect(screen.getByText("Corrected result only")).toBeVisible();
    expect(screen.getByRole("table", {
      name: "After correction allowed memory versions",
    })).toHaveTextContent("memory-restored");
  });

  it("renders the four recorded causal nodes before any raw identity", () => {
    render(<CausalRail scenario={scenario} snapshot={snapshot} activeRun={null} />);

    const rail = screen.getByRole("list", { name: "Signature replay chronology" });
    expect(within(rail).getAllByRole("listitem")).toHaveLength(4);
    expect(within(rail).getByText("Cited belief")).toBeVisible();
    expect(within(rail).getByText("Pre-correction recommendation")).toBeVisible();
    expect(within(rail).getByText("Audited rewind")).toBeVisible();
    expect(within(rail).getByText("Post-correction recommendation")).toBeVisible();
    expect(
      screen.getByRole("heading", {
        name: "Recorded action changed after correction.",
      }),
    ).toBeVisible();
    expect(within(rail).getByText("scale_workers")).toBeVisible();
    expect(within(rail).getByText("throttle_retries")).toBeVisible();
    expect(within(rail).getByText(/Stale guidance recommends scaling workers/)).toBeVisible();
    expect(screen.getByLabelText(/Copy pre-correction decision/)).toHaveAttribute(
      "title",
      "decision-rejected",
    );
  });

  it("uses the newest active run instead of resurrecting an older completed result", () => {
    const activeRun = {
      id: "run-latest-active",
      status: "awaiting_approval",
      decision_id: "decision-latest-active",
      proposed_action: "Inspect the latest processor state before approving a change.",
    };
    render(
      <CausalRail
        scenario={{
          ...scenario,
          status: "active",
          completed_at: null,
          stages: { ...scenario.stages, corrected_decision_id: null },
        }}
        snapshot={snapshot}
        activeRun={activeRun}
      />,
    );

    expect(screen.getByText("Rerun recommendation")).toBeVisible();
    expect(screen.getByText(/Inspect the latest processor state/)).toBeVisible();
  });

  it("does not claim an action delta when the strict comparison is unavailable", () => {
    render(
      <CausalRail
        scenario={{
          ...scenario,
          action_comparison: {
            ...scenario.action_comparison!,
            status: "unavailable",
            contract: null,
            controlled_pair: false,
          },
        }}
        snapshot={snapshot}
        activeRun={null}
      />,
    );

    expect(
      screen.getByRole("heading", {
        name: "Causal comparison unavailable.",
      }),
    ).toBeVisible();
    expect(
      screen.queryByText("Recorded action changed after correction."),
    ).not.toBeInTheDocument();
  });

  it("does not present a pre-rewind active run as the rerun", () => {
    render(
      <CausalRail
        scenario={{
          ...scenario,
          status: "active",
          completed_at: null,
          runs: [],
          operation: null,
          stages: {
            baseline_memory_id: "memory-baseline",
            compromised_memory_id: "memory-compromised",
          },
        }}
        snapshot={{ ...snapshot, operations: [] }}
        activeRun={{
          id: "run-first-active",
          status: "awaiting_approval",
          decision_id: "decision-first-active",
          proposed_action: "First-run recommendation must not appear in the rerun node.",
        }}
      />,
    );

    expect(screen.queryByText(/First-run recommendation/)).not.toBeInTheDocument();
  });

  it("uses recorded read justification when belief content is unavailable", () => {
    const metadataOnlySnapshot = {
      ...snapshot,
      memories: snapshot.memories.map((memory) =>
        memory.id === "memory-compromised" ? { ...memory, content: null } : memory,
      ),
    };
    render(
      <CausalRail scenario={scenario} snapshot={metadataOnlySnapshot} activeRun={null} />,
    );

    expect(screen.getByText(/Previously approved retry guidance is stale/)).toBeVisible();
  });

  it("keeps historical and current outcomes together in structured plan sections", () => {
    render(<OutcomeComparison scenario={scenario} activeRun={null} />);

    expect(screen.getByText("Historical outcome")).toBeVisible();
    expect(screen.getByText("Current outcome")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Rejected recommendation" })).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "Post-correction recommendation" }),
    ).toBeVisible();
    expect(screen.getAllByText("Cause")).toHaveLength(2);
    expect(screen.getAllByText("Checks")).toHaveLength(2);
    expect(screen.getAllByText("Action")).toHaveLength(2);
    expect(screen.getAllByText("Approval outcome")).toHaveLength(2);
    expect(screen.getByText("Rejected by operator")).toBeVisible();
    expect(screen.getByText("not executed")).toBeVisible();
    expect(screen.getByText("recommendation approved")).toBeVisible();
    expect(screen.getAllByText(/gemini \/ gemini-2.5-flash/)).toHaveLength(2);
    expect(screen.getAllByText("payments.retry_fanout")).toHaveLength(2);
    expect(
      screen.getAllByText(/Hindsight\/ControlledIncidentTelemetry \/ RetryFanout \/ 2 datapoints/),
    ).toHaveLength(2);
    expect(screen.getByText(/Throttle retry fanout while processor health recovers/)).toBeVisible();
    expect(screen.getByText("demo.fixture-import")).toBeVisible();
    expect(screen.getByText("demo:stale-runbook-import")).toBeVisible();
    expect(screen.getByText(/Previously approved retry guidance is stale/)).toBeVisible();
    expect(screen.getByText("demo.seed")).toBeVisible();
    expect(screen.getAllByText("1 downstream lineage edge")).toHaveLength(2);
    expect(screen.getByLabelText(/Copy historical cited memory/)).toHaveAttribute(
      "title",
      "memory-compromised",
    );
    expect(screen.getByLabelText(/Copy current cited memory/)).toHaveAttribute(
      "title",
      "memory-restored",
    );
  });

  it("shows the recommendation identity before approval", () => {
    const { container } = render(
      <OutcomeComparison
        scenario={null}
        activeRun={{
          id: "run-awaiting-approval",
          status: "awaiting_approval",
          action_trace: {
            mode: "recommendation_only",
            selection: {
              fingerprint: "b".repeat(64),
              provider: "gemini",
              model: "gemini-2.5-flash",
            },
            recommendation: {
              id: `recommendation:${"a".repeat(64)}`,
              summary: "Inspect processor health before changing retry capacity.",
            },
            execution: { status: "awaiting_approval", mode: "recommendation_only" },
          },
        }}
      />,
    );

    const request = container.querySelector(".action-execution");
    expect(request).toHaveAttribute("data-execution-status", "awaiting_approval");
    expect(request).toHaveTextContent("gemini / gemini-2.5-flash");
  });

  it("shows the governed retraction target, preview, and completed result", () => {
    const { container } = render(
      <OutcomeComparison
        scenario={null}
        activeRun={{
          id: "run-action",
          status: "completed",
          action_trace: remediationActionTrace,
        }}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "Completed governed-memory retraction" }),
    ).toBeVisible();
    const result = container.querySelector(".action-execution");
    expect(result).toHaveTextContent("Increase retry fanout during saturation.");
    expect(result).toHaveTextContent("2 bounded mutations");
    expect(result).toHaveTextContent("Close memory memory-unsafe");
    expect(result).toHaveTextContent(
      "Resolve review review-unsafe for memory memory-unsafe as superseded",
    );
    expect(result).toHaveTextContent("Preview dddddddd");
    expect(result).toHaveTextContent("Operation operation-1: completed");
  });

  it("names a rejected governed retraction without relabeling ordinary recommendations", () => {
    const rejectedRemediation: SignatureScenario = {
      ...scenario,
      runs: [
        {
          ...scenario.runs[0],
          action_trace: {
            ...remediationActionTrace,
            approval: { approved: false, disposition: "rejected" },
            execution: {
              status: "not_executed",
              mode: "governed_memory_remediation",
            },
          },
        },
        scenario.runs[1],
      ],
    };

    render(<OutcomeComparison scenario={rejectedRemediation} activeRun={null} />);

    expect(
      screen.getByRole("heading", { name: "Rejected governed-memory retraction" }),
    ).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "Post-correction recommendation" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "Rejected recommendation" }),
    ).not.toBeInTheDocument();
  });

  it("renders model Markdown without exposing syntax as the primary presentation", () => {
    const markdownScenario: SignatureScenario = {
      ...scenario,
      runs: scenario.runs.map((run) =>
        run.status === "completed"
          ? {
              ...run,
              plan: `## Suspected Cause
### Evidence
**Retry fanout** amplified processor timeouts.

## Checks
- Inspect \`queue_depth\`
- Compare current processor health

## Safe Next Action
[Throttle retries](https://example.com/runbook) before scaling.`,
              proposed_action: "[Throttle retries](https://example.com/runbook) before scaling.",
            }
          : run,
      ),
    };
    const { container } = render(
      <OutcomeComparison scenario={markdownScenario} activeRun={null} />,
    );

    expect(screen.getByRole("heading", { name: "Evidence", level: 4 })).toBeVisible();
    expect(screen.getByText("Retry fanout").tagName).toBe("STRONG");
    expect(screen.getByText("queue_depth").tagName).toBe("CODE");
    expect(screen.getAllByRole("link", { name: /Throttle retries/ })).not.toHaveLength(0);
    expect(container.querySelector("#planText")?.textContent).not.toContain("##");
    expect(container.querySelector("#proposedAction")).toHaveTextContent("Throttle retries");
  });

  it("exposes current, invalidated, historical, operation, and influence states semantically", () => {
    const historical = { ...snapshot, mode: "as_of" as const, as_of: snapshot.timeline[0] };
    const onSelect = vi.fn();
    const { rerender } = render(
      <>
        <BeliefLedger snapshot={snapshot} />
        <InfluenceLedger
          influence={[
            {
              status: "invalidated",
              memory: snapshot.memories[1],
              read: { id: "read-1", rank: 1 },
            },
          ]}
        />
        <OperationLedger operations={snapshot.operations} />
        <Timeline snapshot={snapshot} onSelect={onSelect} />
      </>,
    );

    expect(screen.getByText("1 live · 1 invalid")).toBeVisible();
    expect(screen.getByText("invalidated")).toBeVisible();
    expect(screen.getByText("rewind · completed")).toBeVisible();
    expect(screen.getByText("1 read")).toBeVisible();
    const slider = screen.getByRole("slider") as HTMLInputElement;
    slider.value = "0";
    act(() => slider.dispatchEvent(new Event("input")));
    expect(onSelect).toHaveBeenCalledWith(snapshot.timeline[0]);

    rerender(<BeliefLedger snapshot={historical} />);
    expect(screen.getByRole("heading", { name: "Beliefs As Of" })).toBeVisible();
  });

  it("shows recorded service, reasoning models, and every embedding profile tied to reads", () => {
    render(
      <StoryHeader
        incident={scenario.incident || null}
        namespace={scenario.namespace}
        run={scenario.runs[1]}
        scenario={scenario}
      />,
    );

    expect(screen.getByText("payments-api")).toBeVisible();
    expect(screen.getAllByText("gemini / gemini-2.5-flash")).not.toHaveLength(0);
    expect(screen.getByText(/profile-gemini-001/)).toBeVisible();
    expect(screen.getByText(/profile-gemini-002/)).toBeVisible();
  });

  it("distinguishes decision-evidence loading, error, and empty states", () => {
    const { rerender } = render(<InfluenceLedger influence={[]} state="loading" />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading decision evidence");

    rerender(<InfluenceLedger influence={[]} state="error" error="trace read failed" />);
    expect(screen.getByRole("alert")).toHaveTextContent("trace read failed");

    rerender(<InfluenceLedger influence={[]} state="empty" />);
    expect(screen.getByText("No recorded reads")).toBeVisible();
  });

  it("provides explicit loading and retryable failure surfaces", () => {
    const retry = vi.fn();
    const { rerender } = render(<LoadingSurface />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading governed memory replay");

    rerender(<ErrorSurface message="trace unavailable" onRetry={retry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("trace unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Retry trace" }));
    expect(retry).toHaveBeenCalledOnce();
  });
});
