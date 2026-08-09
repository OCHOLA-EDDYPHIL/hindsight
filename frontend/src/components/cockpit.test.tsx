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
  Timeline,
} from "@/components/cockpit";
import type { SignatureScenario, Snapshot } from "@/types";

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
      trace: {
        reads: [
          {
            id: "read-compromised",
            memory_id: "memory-compromised",
            writer: "demo.fixture-import",
            source_ref: "demo:stale-runbook-import",
            justification: "Previously approved retry guidance is stale for this incident.",
            outgoing_lineage_edge_ids: ["edge-compromised-reflection"],
          },
        ],
      },
      action_trace: {
        schema_version: 2,
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
        },
        execution: { status: "not_executed", mode: "recommendation_only" },
        tool_calls: [
          {
            id: "diagnostic-rejected",
            tool: "aws_cloudwatch_diagnostics",
            query_key: "payments.retry_fanout",
            status: "completed",
          },
        ],
        observations: [
          {
            id: "observation-rejected",
            tool_call_id: "diagnostic-rejected",
            schema_version: 1,
            tool: "aws_cloudwatch_diagnostics",
            query_key: "payments.retry_fanout",
            metric: { namespace: "Hindsight/ControlledIncidentTelemetry", name: "RetryFanout" },
            datapoint_count: 12,
          },
        ],
      },
    },
    {
      id: "run-corrected",
      status: "completed",
      decision_id: "decision-corrected",
      plan: "Retry fanout amplified processor timeouts; inspect queue depth; throttle retry workers.",
      proposed_action: "Throttle retry fanout while processor health recovers.",
      trace: {
        reads: [
          {
            id: "read-baseline",
            memory_id: "memory-baseline",
            writer: "demo.seed",
            source_ref: "demo:known-good-payment-incident",
            justification: "Resolved incident evidence supports throttling retries.",
            outgoing_lineage_edge_ids: ["edge-baseline-reflection"],
          },
        ],
      },
      action_trace: {
        schema_version: 2,
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
        },
        execution: { status: "recommendation_approved", mode: "recommendation_only" },
        tool_calls: [
          {
            id: "diagnostic-corrected",
            tool: "aws_cloudwatch_diagnostics",
            query_key: "payments.processor_queue_depth",
            status: "completed",
          },
        ],
        observations: [
          {
            id: "observation-corrected",
            tool_call_id: "diagnostic-corrected",
            schema_version: 1,
            tool: "aws_cloudwatch_diagnostics",
            query_key: "payments.processor_queue_depth",
            metric: {
              namespace: "Hindsight/ControlledIncidentTelemetry",
              name: "ProcessorQueueDepth",
            },
            datapoint_count: 10,
          },
        ],
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

  it("keeps historical and current outcomes together in structured plan sections", () => {
    render(<OutcomeComparison scenario={scenario} activeRun={null} />);

    expect(screen.getByText("Historical outcome")).toBeVisible();
    expect(screen.getByText("Current outcome")).toBeVisible();
    expect(screen.getAllByText("Cause")).toHaveLength(2);
    expect(screen.getAllByText("Checks")).toHaveLength(2);
    expect(screen.getAllByText("Action")).toHaveLength(2);
    expect(screen.getAllByText("Safety")).toHaveLength(2);
    expect(screen.getByText("not executed")).toBeVisible();
    expect(screen.getByText("recommendation approved")).toBeVisible();
    expect(screen.getAllByText(/gemini \/ gemini-2.5-flash/)).toHaveLength(2);
    expect(screen.getByText("payments.retry_fanout")).toBeVisible();
    expect(screen.getByText(/Hindsight\/ControlledIncidentTelemetry \/ RetryFanout \/ 12 datapoints/)).toBeVisible();
    expect(screen.getByText("payments.processor_queue_depth")).toBeVisible();
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
      "memory-baseline",
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
