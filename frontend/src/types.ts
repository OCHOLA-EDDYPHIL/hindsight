export type Identifier = string;

export interface HostedUiAuthConfig {
  hostedUiBaseUrl: string;
  clientId: string;
  redirectUri: string;
  logoutUri: string;
  scopes: string[];
}

export interface RuntimeConfig {
  publicApiBase?: string;
  productApiBase?: string;
  protectedApiBase?: string;
  auth?: HostedUiAuthConfig | null;
  snapshotBase?: string | null;
  websocketUrl?: string | null;
  defaultNamespace?: string;
  pollIntervalMs?: number;
  operationPollSeconds?: number;
}

export type ProductRole = "viewer" | "operator";
export type AuthStatus = "initializing" | "public" | "authenticated";

export interface EffectiveIdentity {
  principal_id: Identifier;
  tenant_id: Identifier;
  tenant_slug: string;
  token_role: ProductRole;
  mapped_role: ProductRole;
  effective_role: ProductRole;
  scopes: string[];
  expires_at: number;
}

export type RealtimeEventType = "memory" | "operation" | "run" | "run_event";

export interface RealtimeCursor {
  hlc: string;
  event_id: Identifier;
}

export interface RealtimeEnvelopeV2 {
  version: 2;
  event_id: Identifier;
  cursor: RealtimeCursor;
  type: RealtimeEventType;
  namespace?: string | null;
  run_id?: Identifier | null;
  data: Record<string, unknown>;
}

export interface Incident {
  id?: Identifier;
  slug: string;
  title: string;
  summary?: string | null;
  severity?: string | null;
  service_slug?: string | null;
  runs?: Array<{
    id: Identifier;
    status?: string;
    created_at?: string | null;
    updated_at?: string | null;
  }>;
  latest_run_status?: string | null;
}

export interface Retrieval {
  id: Identifier;
  embedding_profile_id?: Identifier | null;
  embedding_provider?: string | null;
  embedding_model?: string | null;
  selected_strategy?: string | null;
  status?: string | null;
  max_distance?: number | null;
}

export interface TraceRead {
  id: Identifier;
  memory_id: Identifier;
  retrieval_id?: Identifier | null;
  belief_id?: Identifier | null;
  version_number?: number | null;
  rank?: number | null;
  distance?: number | null;
  memory_status?: string | null;
  writer?: string | null;
  source_ref?: string | null;
  justification?: string | null;
  embedding_profile_id?: Identifier | null;
  evidence_ids?: Identifier[];
  incoming_lineage_edge_ids?: Identifier[];
  outgoing_lineage_edge_ids?: Identifier[];
}

export interface DecisionTrace {
  decision?: {
    id: Identifier;
    status?: string | null;
    purpose?: string | null;
    opened_at?: string | null;
    sealed_at?: string | null;
  } | null;
  retrievals?: Retrieval[];
  reads?: TraceRead[];
  evidence?: Array<{ id: Identifier }>;
  lineage_edges?: Array<{ id: Identifier }>;
}

export interface DiagnosticToolCall {
  id: Identifier;
  tool: "aws_cloudwatch_diagnostics";
  query_key: string;
  status: "executing" | "completed";
}

export interface DiagnosticObservation {
  id: Identifier;
  tool_call_id: Identifier;
  schema_version: number;
  tool: "aws_cloudwatch_diagnostics";
  query_key: string;
  query_fingerprint?: string;
  region?: string;
  metric?: {
    namespace?: string;
    name?: string;
    dimensions?: Array<{ name: string; value: string }>;
    statistic?: string;
    unit?: string;
    period_seconds?: number;
  };
  window?: {
    start?: string;
    end?: string;
    seconds?: number;
  };
  datapoints?: Array<{ timestamp: string; value: number }>;
  datapoint_count: number;
  truncated?: boolean;
}

export interface CausalEnvelope {
  schema_version: number;
  canonicalization: string;
  identity: {
    scenario_id?: Identifier;
    namespace?: string;
    replay_anchor?: string;
    scenario_routing_key?: string;
    release_revision?: string;
  };
  invariant_inputs: {
    ordered_observations?: DiagnosticObservation[];
    ordered_model_request_configuration?: Array<Record<string, unknown>>;
    embedding_profile?: Record<string, unknown>;
    release_revision?: string;
    action_catalog?: Record<string, unknown>;
  };
  invariant_inputs_sha256: string;
  permitted_intervention: {
    kind?: string;
    ordered_memory_versions?: Array<{
      ordinal?: number;
      memory?: {
        memory_id?: Identifier;
        belief_id?: Identifier | null;
        version?: number | null;
      };
      memory_sha256?: string;
      prompt_fragment_sha256?: string;
    }>;
    selection_fingerprint?: string;
    expected_changed_prompt_fragments?: string[];
    correction_operation_id?: Identifier | null;
    correction_target_timestamp?: string | null;
    operation_effects?: OperationEffect[];
    invalidated_memory_fingerprints?: string[];
    restored_memory_fingerprints?: string[];
  };
  rendered_prompt_sha256?: string[];
  envelope_sha256: string;
}

export interface RecommendationActionTrace {
  schema_version?: number;
  mode?: "recommendation_only" | "governed_memory_remediation";
  selection?: {
    fingerprint?: string;
    memory_ids?: Identifier[];
    provider?: string | null;
    model?: string | null;
  };
  reasoning_steps?: Array<{
    turn?: number;
    provider?: string;
    model?: string;
    request?: Record<string, unknown> | null;
    requests?: Array<Record<string, unknown>>;
    decision?: Record<string, unknown>;
  }>;
  tool_calls?: DiagnosticToolCall[];
  observations?: DiagnosticObservation[];
  causal_envelope?: CausalEnvelope;
  recommendation?: {
    id?: Identifier;
    summary?: string | null;
    diagnosis?: string | null;
    rationale?: string | null;
    rollback?: string | null;
    verification?: string[];
    safety_constraints?: string[];
    status?: string;
    operational_action?: OperationalAction;
  };
  observation_fingerprint?: string;
  remediation_action?: {
    id?: Identifier;
    name?: "retract_recalled_memory";
    target_memory_id?: Identifier;
    target_excerpt?: string;
    reason?: string;
    diagnosis?: string | null;
    rationale?: string | null;
    rollback?: string | null;
    verification?: string[];
    safety_constraints?: string[];
    status?: string;
  };
  preview?: {
    id?: Identifier;
    fingerprint?: string;
    expires_at?: string | null;
    effect_count?: number;
    effects?: {
      close_memory_ids?: Identifier[];
      review_resolutions?: Array<{
        id?: Identifier;
        semantic_memory_id?: Identifier;
        status?: string;
      }>;
    };
  };
  approval?: {
    approved?: boolean;
    disposition?: string;
    recommendation_id?: Identifier;
    remediation_action_id?: Identifier;
    selection_fingerprint?: string;
    observation_fingerprint?: string;
    preview_id?: Identifier;
    preview_fingerprint?: string;
    actor?: string;
  };
  execution?: {
    status?:
      | "awaiting_approval"
      | "recommendation_approved"
      | "approved"
      | "completed"
      | "not_executed"
      | "replan_required";
    mode?: "recommendation_only" | "governed_memory_remediation";
    operation_id?: Identifier;
    operation_status?: string;
    events?: Array<Record<string, unknown>>;
    effects?: Array<Record<string, unknown>>;
  };
}

export interface OperationalAction {
  catalog_id: "payments_retry_amplification.actions.v1";
  contract: "payments_retry_amplification.v1";
  action_id: "scale_workers" | "throttle_retries" | "inspect_only";
  disposition: "recommend";
  parameters: Record<string, never>;
  primary_action: "scale_workers" | "throttle_retries" | "inspect_only";
  directive: string;
  consistency_status: "consistent";
  fingerprint: string;
}

export interface CausalProofState {
  status: "proven" | "not_proven" | "unavailable";
  reason: string;
}

export interface ControlledPairCheck {
  field: string;
  status: "matched" | "mismatched" | "unavailable";
  reason: string;
}

export interface CausalEvidenceSummary {
  schema_version: 1;
  canonicalization: "hindsight.canonical-json.v1";
  scope: "recommendation_only";
  proof_states: {
    memory_correction_proven: CausalProofState;
    action_delta_proven: CausalProofState;
    controlled_pair_eligible: CausalProofState;
    repeatable_causal_effect_supported: CausalProofState;
    service_recovery_proven: CausalProofState;
  };
  controlled_pair_checks?: ControlledPairCheck[];
  before_envelope_sha256?: string | null;
  after_envelope_sha256?: string | null;
  download?: {
    url: string;
    protected_url: string;
    sha256: string;
    media_type: "application/json";
  };
}

export interface ActionComparison {
  status: "changed" | "unchanged" | "unavailable";
  contract: "payments_retry_amplification.v1" | null;
  before: (OperationalAction & { decision_id: Identifier }) | null;
  after: (OperationalAction & { decision_id: Identifier }) | null;
  context: {
    prompt_equal: boolean;
    normalized_telemetry_equal: boolean;
  };
  memory_correction_proven: boolean;
  controlled_pair: boolean;
}

export interface Run {
  id: Identifier;
  incident_slug?: string | null;
  namespace?: string | null;
  service_slug?: string | null;
  status: string;
  decision_id?: Identifier | null;
  plan?: string | null;
  proposed_action?: string | null;
  action_approved?: boolean | null;
  provider?: string | null;
  model?: string | null;
  reflected_memory_id?: Identifier | null;
  user_input?: string | null;
  created_at?: string | null;
  completed_at?: string | null;
  events?: Array<{ phase: string; created_at?: string }>;
  trace?: DecisionTrace | null;
  action_trace?: RecommendationActionTrace | null;
}

export interface MemoryRecord {
  id: Identifier;
  namespace?: string;
  belief_id?: Identifier | null;
  version_number?: number | null;
  previous_version_id?: Identifier | null;
  producer_decision_id?: Identifier | null;
  content?: string | null;
  content_schema?: string | null;
  lineage_status?: string | null;
  trust_status?: string | null;
  writer?: string | null;
  source_ref?: string | null;
  justification?: string | null;
  status?: string | null;
  t_valid?: string | null;
  t_invalid?: string | null;
  written_at?: string | null;
  invalidated_at?: string | null;
  invalidation_reason?: string | null;
}

export interface OperationEffect {
  sequence?: number;
  effect_type: string;
  source_memory_id?: Identifier | null;
  result_memory_id?: Identifier | null;
  belief_id?: Identifier | null;
  namespace?: string | null;
}

export interface MemoryOperation {
  id: Identifier;
  operation_type: string;
  status: string;
  reason?: string | null;
  actor?: string | null;
  namespace?: string | null;
  target_timestamp?: string | null;
  invalidated_memory_ids?: Identifier[];
  restored_memory_ids?: Identifier[];
  effects?: OperationEffect[];
  created_at?: string | null;
  completed_at?: string | null;
  failure_code?: string | null;
  failure_detail?: string | null;
}

export interface ConsolidationCandidate {
  candidate_id: Identifier;
  candidate_memory_id: Identifier;
  incident_id: Identifier;
  incident_slug: string;
  incident_title: string;
  namespace: string;
  content: string;
  content_schema: "procedural_lesson.v1";
  structured_payload: Record<string, unknown>;
  trust_status: "review_required";
  review_status: "pending" | "approved" | "rejected";
  candidate_fingerprint: string;
  evidence_fingerprint: string;
  evidence: Array<{
    evidence_id: Identifier;
    relationship: string;
    content: string | null;
    sha256: string;
    current_sha256: string | null;
    matches_manifest: boolean;
    trust_status?: string;
    lineage_status?: string;
    current: boolean;
  }>;
  reviewed_by?: string | null;
  review_reason?: string | null;
  reviewed_at?: string | null;
  approved_memory_id?: Identifier | null;
  created_at: string;
  updated_at: string;
}

export interface ConsolidationReviewPreview {
  id: Identifier;
  operation_type: "consolidation_approval";
  fingerprint: string;
  expires_at: string;
  request_payload: {
    candidate_id: Identifier;
    candidate_memory_id: Identifier;
    candidate_fingerprint: string;
    evidence_fingerprint: string;
    namespace: string;
    action: "approve" | "reject";
    reason: string;
  };
  effect_payload: {
    candidate_memory_id: Identifier;
    review_action: "approve" | "reject";
    namespace: string;
  };
}

export interface Snapshot {
  mode: "current" | "as_of";
  namespace: string;
  as_of?: string | null;
  memories: MemoryRecord[];
  operations: MemoryOperation[];
  timeline: string[];
  generated_at?: string;
}

export interface SignatureScenario {
  scenario_id: Identifier;
  namespace: string;
  status: string;
  session_status: string | null;
  rewind_anchor: string | null;
  created_at?: string | null;
  completed_at: string | null;
  incident?: Incident | null;
  runs: Run[];
  operation?: MemoryOperation | null;
  operation_events?: Array<{
    id: Identifier;
    sequence: number;
    status: string;
    summary?: string | null;
    created_at?: string | null;
  }>;
  operation_effects?: OperationEffect[];
  memories: MemoryRecord[];
  action_comparison?: ActionComparison;
  causal_evidence?: CausalEvidenceSummary;
  stages: {
    baseline_memory_id?: Identifier | null;
    compromised_memory_id?: Identifier | null;
    /** Compatibility alias for replay snapshots created before the role-free scenario contract. */
    poison_memory_id?: Identifier | null;
    influenced_decision_id?: Identifier | null;
    rewind_operation_id?: Identifier | null;
    corrected_decision_id?: Identifier | null;
  };
}

export interface InfluenceItem {
  read?: {
    id?: Identifier;
    read_at?: string | null;
    reader?: string | null;
    rank?: number | null;
    distance?: number | null;
  };
  memory?: MemoryRecord;
  provenance?: MemoryRecord;
  status?: string;
}

export interface RewindPreview {
  id: Identifier;
  fingerprint: string;
  expires_at?: string;
  effect_payload?: {
    close_memory_ids?: Identifier[];
    reassertions?: unknown[];
  };
}

declare global {
  interface Window {
    HINDSIGHT_CONFIG?: RuntimeConfig;
    __HINDSIGHT_CONSOLE_ERRORS?: unknown[];
    __HINDSIGHT_VISIBLE_ERRORS?: unknown[];
  }
}
