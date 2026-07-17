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
      plan: "Rotate certificates; inspect the certificate chain; restart the edge service.",
      proposed_action: "Rotate certificates immediately.",
    },
    {
      id: "run-corrected",
      status: "completed",
      decision_id: "decision-corrected",
      plan: "Retry fanout amplified processor timeouts; inspect queue depth; throttle retry workers.",
      proposed_action: "Throttle retry fanout while processor health recovers.",
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
      content: "Poisoned memory recommends certificate rotation.",
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
    expect(screen.getByText(/Throttle retry fanout while processor health recovers/)).toBeVisible();
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
