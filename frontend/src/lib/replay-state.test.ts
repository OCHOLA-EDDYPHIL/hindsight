import { describe, expect, it } from "vitest";

import {
  influenceFromRun,
  latestScenarioRun,
  readReplayLocation,
} from "@/lib/replay-state";

describe("replay state", () => {
  it("reads durable replay identity from the URL", () => {
    expect(
      readReplayLocation(
        "?scenario_id=scenario-7&namespace=tenant%3Apayments&as_of=2026-08-09T12%3A00%3A00Z",
        "fallback",
      ),
    ).toEqual({
      scenarioId: "scenario-7",
      namespace: "tenant:payments",
      asOf: "2026-08-09T12:00:00Z",
    });
  });

  it("selects a newer active run over an older completed run", () => {
    expect(
      latestScenarioRun([
        {
          id: "completed-old",
          status: "completed",
          created_at: "2026-08-09T11:00:00Z",
        },
        {
          id: "active-new",
          status: "running",
          created_at: "2026-08-09T12:00:00Z",
        },
      ])?.id,
    ).toBe("active-new");
  });

  it("preserves recorded provenance when adapting scenario reads", () => {
    expect(
      influenceFromRun({
        id: "run-1",
        status: "completed",
        trace: {
          reads: [
            {
              id: "read-1",
              memory_id: "memory-1",
              writer: "postmortem.import",
              source_ref: "incident:42",
              justification: "Verified recovery evidence",
            },
          ],
        },
      })[0]?.memory,
    ).toMatchObject({
      id: "memory-1",
      writer: "postmortem.import",
      source_ref: "incident:42",
      justification: "Verified recovery evidence",
    });
  });
});
