import { describe, expect, it } from "vitest";

import { structurePlan } from "@/lib/format";

describe("structurePlan", () => {
  it("maps Markdown-labelled plan sections without discarding formatting", () => {
    const plan = structurePlan({
      id: "run-1",
      status: "awaiting_approval",
      plan: `## Suspected Cause
**Retry fanout** amplified timeouts.

## Checks
- Inspect \`queue_depth\`
- Compare [processor health](https://example.com/health)

## Safe Next Action
Throttle retries before scaling.`,
    });

    expect(plan.cause).toContain("**Retry fanout**");
    expect(plan.checks).toContain("- Inspect `queue_depth`");
    expect(plan.action).toBe("Throttle retries before scaling.");
    expect(plan.recordedPlan).toBeNull();
  });

  it("keeps an unstructured plan recorded without inventing cause, checks, or action", () => {
    const plan = structurePlan({
      id: "run-2",
      status: "completed",
      plan: "**Retry fanout** is high; inspect the queue; throttle retries.",
    });

    expect(plan.recordedPlan).toBe(
      "**Retry fanout** is high; inspect the queue; throttle retries.",
    );
    expect(plan.cause).toBeNull();
    expect(plan.checks).toBeNull();
    expect(plan.action).toBeNull();
  });

  it("returns null evidence fields when no plan was recorded", () => {
    expect(structurePlan(null)).toEqual({
      cause: null,
      checks: null,
      action: null,
      recordedPlan: null,
    });
  });
});
