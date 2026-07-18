import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { deriveWalkthroughStep, OperatorConsole } from "@/components/operator-console";
import type { Snapshot } from "@/types";

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
  snapshot: baselineSnapshot,
  rewindTimestamp: "",
  rewindReason: "Remove poisoned guidance",
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
  onLock: vi.fn(),
};

describe("operator separation", () => {
  it("keeps mutation controls inert and outside the visible public replay", () => {
    render(<OperatorConsole {...props} operator={false} />);

    expect(screen.getByRole("region", { hidden: true })).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByRole("button", { name: "Analyze incident", hidden: true })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Execute rewind", hidden: true })).toBeDisabled();
  });

  it("labels preview and execution as distinct operator states", () => {
    render(
      <OperatorConsole
        {...props}
        operator
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

  it("keeps the walkthrough optional and never invokes mutation callbacks", () => {
    render(<OperatorConsole {...props} operator />);

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
    const poisonSnapshot: Snapshot = {
      ...baselineSnapshot,
      memories: [
        ...baselineSnapshot.memories,
        { id: "poison", writer: "demo.poison", status: "current" },
      ],
    };
    const rewoundSnapshot: Snapshot = {
      ...poisonSnapshot,
      memories: poisonSnapshot.memories.map((memory) =>
        memory.writer === "demo.poison"
          ? { ...memory, status: "invalidated", t_invalid: "2026-07-18T00:00:00Z" }
          : memory,
      ),
      operations: [{ id: "rewind", operation_type: "rewind", status: "completed" }],
    };
    const state = {
      operator: true,
      rewindAnchor: "2026-07-18T00:00:00Z",
      snapshot: poisonSnapshot,
      run: null,
      rewindPreview: null,
    };

    expect(deriveWalkthroughStep({ ...state, operator: false })).toBe("unlock");
    expect(deriveWalkthroughStep({ ...state, rewindAnchor: null })).toBe("reset");
    expect(deriveWalkthroughStep({ ...state, snapshot: baselineSnapshot })).toBe("poison");
    expect(deriveWalkthroughStep(state)).toBe("analyze");
    expect(
      deriveWalkthroughStep({ ...state, run: { id: "bad", status: "awaiting_approval" } }),
    ).toBe("reject");
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
        run: { id: "bad", status: "rejected" },
      }),
    ).toBe("reanalyze");
    expect(
      deriveWalkthroughStep({
        ...state,
        snapshot: rewoundSnapshot,
        run: { id: "good", status: "completed" },
      }),
    ).toBe("history");
  });
});
