import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { deriveWalkthroughStep, OperatorConsole } from "@/components/operator-console";
import type { SignatureScenario, Snapshot } from "@/types";

const baselineSnapshot: Snapshot = {
  mode: "current",
  namespace: "demo:session:one",
  memories: [{ id: "baseline", writer: "demo.seed", status: "current" }],
  operations: [],
  timeline: [],
};

const props = {
  incidents: [],
  incident: null,
  run: null,
  incidentInput: "processor timeout report",
  busy: null,
  rewindAnchor: null,
  scenario: null,
  snapshot: baselineSnapshot,
  rewindTimestamp: "",
  rewindReason: "Remove stale guidance",
  rewindPreview: null,
  onIncident: vi.fn(),
  onIncidentInput: vi.fn(),
  onReset: vi.fn(),
  onPoison: vi.fn(),
  onRun: vi.fn(),
  onDecision: vi.fn(),
  onRewindTimestamp: vi.fn(),
  onRewindReason: vi.fn(),
  onPreview: vi.fn(),
  onExecute: vi.fn(),
  onSignOut: vi.fn(),
};

describe("operator console", () => {
  it("labels preview and execution as distinct operator states", () => {
    render(
      <OperatorConsole
        {...props}
        rewindPreview={{
          id: "preview-1",
          fingerprint: "fingerprint-1",
          effect_payload: { close_memory_ids: ["memory-1", "memory-2"], reassertions: [] },
        }}
      />,
    );

    expect(screen.getByText("2 versions will close.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Preview" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Execute rewind" })).toBeEnabled();
  });

  it("shows the complete governed action and observation sequence", () => {
    render(
      <OperatorConsole
        {...props}
        run={{
          id: "run-1",
          status: "reflecting",
          events: [
            { phase: "triage" },
            { phase: "recall" },
            { phase: "plan" },
            { phase: "approval" },
            { phase: "action" },
            { phase: "observation" },
            { phase: "reflection" },
          ],
        }}
      />,
    );

    const rail = screen.getByRole("list", { name: "Agent run phases" });
    expect(rail.querySelectorAll("li")).toHaveLength(7);
    expect(screen.getByText("cited proposal")).toBeVisible();
    expect(screen.getByText("recommendation")).toBeVisible();
    expect(screen.getByText("diagnostics")).toBeVisible();
  });

  it("marks the final observed phase complete for terminal runs and failed for failures", () => {
    const { rerender } = render(
      <OperatorConsole
        {...props}
        run={{
          id: "run-complete",
          status: "completed",
          events: [{ phase: "triage" }, { phase: "reflection" }],
        }}
      />,
    );

    expect(document.querySelector('[data-phase="reflection"]')).toHaveAttribute(
      "data-phase-state",
      "complete",
    );

    rerender(
      <OperatorConsole
        {...props}
        run={{ id: "run-failed", status: "failed", events: [{ phase: "triage" }] }}
      />,
    );
    expect(document.querySelector('[data-phase="triage"]')).toHaveAttribute(
      "data-phase-state",
      "failed",
    );
  });

  it("keeps the walkthrough optional and never invokes mutation callbacks", () => {
    render(<OperatorConsole {...props} />);

    expect(screen.getByRole("complementary", { hidden: true })).not.toBeVisible();
    const toggle = screen.getByRole("button", { name: "Walkthrough" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);

    expect(screen.getByRole("complementary")).toBeVisible();
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("list", { name: "Operator replay walkthrough" })).toBeVisible();
    expect(screen.getByText("Reset the replay").closest("li")).toHaveAttribute(
      "aria-current",
      "step",
    );
    expect(screen.getByRole("button", { name: "Reset" })).toHaveAttribute(
      "aria-describedby",
      "walkthroughCurrent",
    );
    for (const callback of [props.onReset, props.onPoison, props.onRun, props.onDecision]) {
      expect(callback).not.toHaveBeenCalled();
    }
  });

  it("derives every walkthrough transition from durable product state", () => {
    const compromisedMemoryId = "compromised-guidance";
    const compromisedScenario = {
      scenario_id: "scenario-1",
      namespace: baselineSnapshot.namespace,
      status: "active",
      session_status: "active",
      rewind_anchor: "2026-07-18T00:00:00Z",
      completed_at: null,
      runs: [],
      memories: [],
      stages: { compromised_memory_id: compromisedMemoryId },
    } satisfies SignatureScenario;
    const compromisedSnapshot: Snapshot = {
      ...baselineSnapshot,
      memories: [
        ...baselineSnapshot.memories,
        { id: compromisedMemoryId, writer: "demo.fixture-import", status: "current" },
      ],
    };
    const rewoundSnapshot: Snapshot = {
      ...compromisedSnapshot,
      memories: compromisedSnapshot.memories.map((memory) =>
        memory.id === compromisedMemoryId
          ? { ...memory, status: "invalidated", t_invalid: "2026-07-18T00:00:00Z" }
          : memory,
      ),
      operations: [{ id: "rewind", operation_type: "rewind", status: "completed" }],
    };
    const state = {
      rewindAnchor: "2026-07-18T00:00:00Z",
      scenario: compromisedScenario,
      snapshot: compromisedSnapshot,
      run: null,
      rewindPreview: null,
    };

    expect(deriveWalkthroughStep({ ...state, rewindAnchor: null })).toBe("reset");
    expect(deriveWalkthroughStep({ ...state, scenario: null, snapshot: baselineSnapshot })).toBe(
      "compromise",
    );
    expect(deriveWalkthroughStep(state)).toBe("analyze");
    expect(
      deriveWalkthroughStep({ ...state, run: { id: "bad", status: "awaiting_approval" } }),
    ).toBe("review");
    expect(deriveWalkthroughStep({ ...state, run: { id: "bad", status: "rejected" } })).toBe(
      "preview",
    );
    expect(
      deriveWalkthroughStep({
        ...state,
        run: { id: "bad", status: "rejected" },
        rewindPreview: { id: "preview", fingerprint: "fingerprint" },
      }),
    ).toBe("execute");
    expect(
      deriveWalkthroughStep({
        ...state,
        snapshot: rewoundSnapshot,
        run: { id: "good", status: "awaiting_approval" },
      }),
    ).toBe("review");
    expect(
      deriveWalkthroughStep({
        ...state,
        snapshot: rewoundSnapshot,
        run: { id: "bad", status: "rejected" },
      }),
    ).toBe("reanalyze");
    expect(
      deriveWalkthroughStep({
        ...state,
        scenario: { ...compromisedScenario, status: "completed" },
        snapshot: rewoundSnapshot,
        run: { id: "good", status: "completed" },
      }),
    ).toBe("history");
  });

  it("blocks approval controls when the recommendation identity is absent", () => {
    render(
      <OperatorConsole
        {...props}
        run={{ id: "run-1", status: "awaiting_approval" }}
      />,
    );

    expect(screen.getByText(/Approval identity unavailable/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Reject recommendation" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Approve recommendation" })).toBeDisabled();
  });

  it("enables approval controls only for a recommendation bound to a selection", () => {
    render(
      <OperatorConsole
        {...props}
        run={{
          id: "run-1",
          status: "awaiting_approval",
          action_trace: {
            mode: "recommendation_only",
            selection: { fingerprint: "b".repeat(64) },
            recommendation: {
              id: `recommendation:${"a".repeat(64)}`,
              summary: "Throttle retry fanout after verifying processor health.",
            },
          },
        }}
      />,
    );

    expect(screen.getByText(/Throttle retry fanout/)).toBeVisible();
    const approve = screen.getByRole("button", { name: "Approve recommendation" });
    expect(approve).toBeEnabled();
    fireEvent.click(approve);
    expect(props.onDecision).toHaveBeenCalledWith(true);
  });
});
