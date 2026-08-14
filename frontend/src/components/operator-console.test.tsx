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

  it("shows the bounded retraction preview before enabling action approval", () => {
    render(
      <OperatorConsole
        {...props}
        run={{
          id: "run-action",
          status: "awaiting_approval",
          action_trace: {
            schema_version: 3,
            mode: "governed_memory_remediation",
            selection: { fingerprint: "b".repeat(64) },
            observation_fingerprint: "c".repeat(64),
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
          },
        }}
      />,
    );

    expect(screen.getByText("Increase retry fanout during saturation.")).toBeVisible();
    expect(screen.getByText(/2 bounded mutations/)).toHaveTextContent("dddddddddddd");
    expect(screen.getByText("Close memory memory-unsafe")).toBeVisible();
    expect(
      screen.getByText("Resolve review review-unsafe for memory memory-unsafe as superseded"),
    ).toBeVisible();
    const approve = screen.getByRole("button", { name: "Approve retraction" });
    expect(approve).toBeEnabled();
    fireEvent.click(approve);
    expect(props.onDecision).toHaveBeenCalledWith(true);
  });

  it("requires a bound preview before executing a generated lesson review", () => {
    const onPreviewCandidateReview = vi.fn();
    const onExecuteCandidateReview = vi.fn();
    const candidateFingerprint = "a".repeat(64);
    const evidenceFingerprint = "b".repeat(64);
    const { rerender } = render(
      <OperatorConsole
        {...props}
        consolidationCandidates={[
          {
            candidate_id: "candidate-1",
            candidate_memory_id: "memory-1",
            incident_id: "incident-1",
            incident_slug: "retry-storm",
            incident_title: "Retry storm",
            namespace: "demo:review",
            content: "Throttle retries after checking downstream health.",
            content_schema: "procedural_lesson.v1",
            structured_payload: {},
            trust_status: "review_required",
            review_status: "pending",
            candidate_fingerprint: candidateFingerprint,
            evidence_fingerprint: evidenceFingerprint,
            evidence: [],
            created_at: "2026-08-14T00:00:00Z",
            updated_at: "2026-08-14T00:00:00Z",
          },
        ]}
        onPreviewCandidateReview={onPreviewCandidateReview}
        onExecuteCandidateReview={onExecuteCandidateReview}
      />,
    );

    expect(screen.queryByRole("button", { name: "Execute bound review" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Preview approval" }));
    expect(onPreviewCandidateReview).toHaveBeenCalledWith(
      "candidate-1",
      "approve",
      expect.any(String),
    );

    rerender(
      <OperatorConsole
        {...props}
        consolidationCandidates={[]}
        consolidationPreview={{
          id: "preview-1",
          operation_type: "consolidation_approval",
          fingerprint: "c".repeat(64),
          expires_at: "2026-08-14T01:00:00Z",
          request_payload: {
            candidate_id: "candidate-1",
            candidate_memory_id: "memory-1",
            candidate_fingerprint: candidateFingerprint,
            evidence_fingerprint: evidenceFingerprint,
            namespace: "demo:review",
            action: "approve",
            reason: "Reviewed evidence",
          },
          effect_payload: {
            candidate_memory_id: "memory-1",
            review_action: "approve",
            namespace: "demo:review",
          },
        }}
        onExecuteCandidateReview={onExecuteCandidateReview}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Execute bound review" }));
    expect(onExecuteCandidateReview).toHaveBeenCalledTimes(1);
  });
});
