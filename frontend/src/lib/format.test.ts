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
  });

  it("does not mistake emphasis markers for list prefixes in fallback text", () => {
    const plan = structurePlan({
      id: "run-2",
      status: "completed",
      plan: "**Retry fanout** is high; inspect the queue; throttle retries.",
    });

    expect(plan.cause).toBe("**Retry fanout** is high");
    expect(plan.checks).toContain("- inspect the queue");
  });
});
