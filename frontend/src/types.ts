export type Identifier = string;

export interface RuntimeConfig {
  apiBase?: string;
  snapshotBase?: string | null;
  websocketUrl?: string | null;
  defaultNamespace?: string;
  pollIntervalMs?: number;
  operationPollSeconds?: number;
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
  runs?: Array<{ id: Identifier; status?: string }>;
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
  account_id?: string;
  region?: string;
  metric?: {
    namespace?: string;
    name?: string;
    dimensions?: Array<{ name: string; value: string }>;
    statistic?: string;
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

export interface RecommendationActionTrace {
  schema_version?: number;
  mode?: "recommendation_only";
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
    decision?: Record<string, unknown>;
  }>;
  tool_calls?: DiagnosticToolCall[];
  observations?: DiagnosticObservation[];
  recommendation?: {
    id?: Identifier;
    summary?: string | null;
    diagnosis?: string | null;
    rationale?: string | null;
    rollback?: string | null;
    verification?: string[];
    safety_constraints?: string[];
    status?: string;
  };
  approval?: {
    approved?: boolean;
    disposition?: string;
    recommendation_id?: Identifier;
    selection_fingerprint?: string;
  };
  execution?: {
    status?:
      | "awaiting_approval"
      | "recommendation_approved"
      | "not_executed"
      | "replan_required";
    mode?: "recommendation_only";
  };
}

export interface Run {
  id: Identifier;
  incident_slug?: string | null;
  namespace?: string | null;
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
  created_at?: string | null;
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
