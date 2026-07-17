import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { OperatorConsole } from "@/components/operator-console";

const props = {
  incidents: [],
  incident: null,
  run: null,
  incidentInput: "processor timeout report",
  busy: null,
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
});
