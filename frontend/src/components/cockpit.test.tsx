import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  BeliefLedger,
  CausalRail,
  ErrorSurface,
  InfluenceLedger,
  LessonIdentityChain,
  LoadingSurface,
  OperationLedger,
  OutcomeComparison,
  Timeline,
} from "@/components/cockpit";
import type { LessonIdentityTrace, SignatureScenario, Snapshot } from "@/types";

const scenario: SignatureScenario = {
  scenario_id: "49109a44-43e7-40de-b547-b4f9d0a387a2",
  namespace: "demo:payments-poison-rewind:session:49109a44",
  status: "completed",
  incident: {
    slug: "demo-payments-checkout-latency:49109a44",
    title: "Checkout latency under retry amplification",
    summary: "A payment processor timeout multiplied checkout retries.",
  },
  runs: [
    {
      id: "run-rejected",
      status: "rejected",
      decision_id: "decision-rejected",
      plan: "Scale payment workers while downstream retry fanout remains elevated.",
      proposed_action: "Scale payment workers while retry fanout remains elevated.",
      action_trace: {
        request: { id: "action-rejected", actions: ["scale_workers"] },
        execution: {
          status: "completed",
          tool: "deterministic_incident_simulator",
        },
        observations: [
          {
            action: "scale_workers",
            unsafe: true,
            recovered: false,
            detail: "scale_workers amplified unresolved upstream pressure",
          },
        ],
        score: { recovered: false, unsafe_action_count: 1 },
      },
    },
    {
      id: "run-corrected",
      status: "completed",
      decision_id: "decision-corrected",
      plan: "Retry fanout amplified processor timeouts; inspect queue depth; throttle retry workers.",
      proposed_action: "Throttle retry fanout while processor health recovers.",
      action_trace: {
        request: {
          id: "action-corrected",
          actions: ["inspect_dependency", "throttle_retries"],
        },
        execution: {
          status: "completed",
          tool: "deterministic_incident_simulator",
        },
        observations: [
          {
            action: "throttle_retries",
            unsafe: false,
            recovered: true,
            detail: "retry fanout throttled; downstream pressure recovered",
          },
        ],
        score: { recovered: true, unsafe_action_count: 0 },
      },
    },
  ],
  operation: {
    id: "operation-rewind",
    operation_type: "rewind",
    status: "completed",
  },
  memories: [],
  stages: {
    baseline_memory_id: "memory-baseline",
    poison_memory_id: "memory-poison",
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
      id: "memory-poison",
      content: "Poisoned memory recommends scaling workers into retry pressure.",
      writer: "demo.poison",
      status: "invalidated",
      t_invalid: "2026-07-17T11:00:00Z",
    },
  ],
  operations: [
    {
      id: "operation-rewind",
      operation_type: "rewind",
      status: "completed",
      reason: "Remove poisoned guidance",
      invalidated_memory_ids: ["memory-poison"],
      restored_memory_ids: [],
    },
  ],
};

const lessonTrace: LessonIdentityTrace = {
  source_incident: { id: "incident-source", slug: "checkout-latency" },
  consolidation: {
    job_id: "consolidation-job",
    producer_decision_id: "decision-producer",
  },
  lesson: {
    memory_id: "memory-lesson",
    belief_id: "belief-lesson",
    version_number: 2,
    embedding_profile_id: "profile-gemini",
  },
  retrieval: {
    retrieval_id: "retrieval-lesson",
    read_id: "read-lesson",
    embedding_profile_id: "profile-gemini",
  },
  embedding_profile: {
    id: "profile-gemini",
    provider: "gemini",
    model: "gemini-embedding-001",
    dimensions: 1024,
    capability: "semantic",
    encoder_revision: "v1",
    max_distance: 0.35,
  },
  lineage_edges: [
    {
      id: "lineage-edge",
      parent_read_id: "read-source",
      parent_memory_id: "memory-source",
      producer_decision_id: "decision-producer",
      edge_type: "derived",
    },
  ],
  consumer_decision: { decision_id: "decision-consumer" },
};

describe("guided replay cockpit", () => {
  it("renders the five durable causal identities in chronology", () => {
    render(<CausalRail scenario={scenario} />);

    const rail = screen.getByRole("list", { name: "Signature replay chronology" });
    expect(within(rail).getAllByRole("listitem")).toHaveLength(5);
    expect(within(rail).getByText("Baseline")).toBeVisible();
    expect(within(rail).getByText("Corrected decision")).toBeVisible();
    expect(screen.getByLabelText(/Copy Influenced decision identity/)).toHaveAttribute(
      "title",
      "decision-rejected",
    );
  });

  it("renders the content-free cross-episode identity chain", () => {
    render(<LessonIdentityChain trace={lessonTrace} state="ready" />);

    const chain = screen.getByRole("list", { name: "Lesson identity chain" });
    expect(within(chain).getAllByRole("listitem")).toHaveLength(7);
    expect(screen.getByText("Version 2")).toBeVisible();
    expect(screen.getByText("1 verified edge")).toBeVisible();
    expect(screen.getByLabelText(/Copy lesson belief identity/)).toHaveAttribute(
      "title",
      "belief-lesson",
    );
    expect(screen.getByLabelText(/Copy embedding profile identity/)).toHaveAttribute(
      "title",
      "profile-gemini",
    );
    expect(screen.getByLabelText(/Copy consumer decision identity/)).toHaveAttribute(
      "title",
      "decision-consumer",
    );
  });

  it("keeps historical and current outcomes together in structured plan sections", () => {
    render(<OutcomeComparison scenario={scenario} activeRun={null} />);

    expect(screen.getByText("Historical outcome")).toBeVisible();
    expect(screen.getByText("Current outcome")).toBeVisible();
    expect(screen.getAllByText("Cause")).toHaveLength(2);
    expect(screen.getAllByText("Checks")).toHaveLength(2);
    expect(screen.getAllByText("Action")).toHaveLength(2);
    expect(screen.getAllByText("Safety")).toHaveLength(2);
    expect(screen.getByText("Not recovered")).toBeVisible();
    expect(screen.getByText("Recovered")).toBeVisible();
    expect(screen.getByText(/1 unsafe · scale_workers/)).toBeVisible();
    expect(screen.getByText(/0 unsafe · inspect_dependency → throttle_retries/)).toBeVisible();
    expect(screen.getAllByText(/deterministic_incident_simulator/)).toHaveLength(2);
    expect(screen.getByText(/deterministic_incident_simulator · scale_workers/)).toBeVisible();
    expect(screen.getByText(/amplified unresolved upstream pressure/)).toBeVisible();
    expect(screen.getByText(/downstream pressure recovered/)).toBeVisible();
    expect(screen.getByText(/Throttle retry fanout while processor health recovers/)).toBeVisible();
  });

  it("shows the bounded action request before execution is approved", () => {
    const { container } = render(
      <OutcomeComparison
        scenario={null}
        activeRun={{
          id: "run-awaiting-approval",
          status: "awaiting_approval",
          action_trace: {
            request: {
              id: "action-awaiting-approval",
              tool: "deterministic_incident_simulator",
              actions: ["scale_workers"],
            },
          },
        }}
      />,
    );

    const request = container.querySelector(".action-execution");
    expect(request).toHaveAttribute("data-execution-status", "awaiting_approval");
    expect(request).toHaveTextContent("deterministic_incident_simulator · scale_workers");
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
