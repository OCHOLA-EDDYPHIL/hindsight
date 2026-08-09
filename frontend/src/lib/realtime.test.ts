import { describe, expect, it } from "vitest";

import {
  BoundedRealtimeTracker,
  compareRealtimeCursors,
  parseRealtimeEnvelopeV2,
} from "@/lib/realtime";
import type { RealtimeEnvelopeV2 } from "@/types";

function envelope(eventId: string, hlc: string): RealtimeEnvelopeV2 {
  return {
    version: 2,
    event_id: eventId,
    cursor: { hlc, event_id: eventId },
    type: "memory",
    namespace: "test:realtime",
    data: { reference: { id: `memory-${eventId}` } },
  };
}

describe("realtime event ordering", () => {
  it("compares CockroachDB HLC components numerically and uses event identity as a tie-breaker", () => {
    expect(
      compareRealtimeCursors(
        { hlc: "10.0000000000", event_id: "event-a" },
        { hlc: "9.9999999999", event_id: "event-z" },
      ),
    ).toBeGreaterThan(0);
    expect(
      compareRealtimeCursors(
        { hlc: "10.12", event_id: "event-a" },
        { hlc: "10.2", event_id: "event-z" },
      ),
    ).toBeGreaterThan(0);
    expect(
      compareRealtimeCursors(
        { hlc: "10.12", event_id: "event-a" },
        { hlc: "10.12", event_id: "event-b" },
      ),
    ).toBeLessThan(0);
  });

  it("rejects malformed v2 identities instead of advancing a high-water mark", () => {
    expect(() =>
      parseRealtimeEnvelopeV2({
        ...envelope("event-a", "10.1"),
        cursor: { hlc: "10.1", event_id: "event-b" },
      }),
    ).toThrow(/must match/);
    expect(() => parseRealtimeEnvelopeV2(envelope("event-a", "not-an-hlc"))).toThrow(
      /HLC is invalid/,
    );
    expect(parseRealtimeEnvelopeV2({ version: 1 })).toBeNull();
  });

  it("deduplicates stable identities without treating unseen reordering as a duplicate", () => {
    const tracker = new BoundedRealtimeTracker();
    const latest = envelope("event-z", "20.0");
    const reordered = envelope("event-a", "19.0");

    expect(tracker.observe("namespace:test", latest)).toBe("forward");
    expect(tracker.observe("namespace:test", latest)).toBe("duplicate");
    expect(tracker.observe("namespace:test", reordered)).toBe("reordered");
    expect(tracker.highWater("namespace:test")).toEqual(latest.cursor);
  });

  it("bounds recent identities and reconciles an evicted replay below high-water", () => {
    const tracker = new BoundedRealtimeTracker(2, 2);
    const first = envelope("event-a", "10.0");

    expect(tracker.observe("namespace:test", first)).toBe("forward");
    expect(tracker.observe("namespace:test", envelope("event-b", "11.0"))).toBe("forward");
    expect(tracker.observe("namespace:test", envelope("event-c", "12.0"))).toBe("forward");
    expect(tracker.observe("namespace:test", first)).toBe("reordered");
  });

  it("tracks independent namespace streams within a bounded registry", () => {
    const tracker = new BoundedRealtimeTracker(8, 2);

    expect(tracker.observe("namespace:a", envelope("event-a", "20.0"))).toBe("forward");
    expect(tracker.observe("namespace:b", envelope("event-b", "10.0"))).toBe("forward");
    expect(tracker.observe("namespace:c", envelope("event-c", "5.0"))).toBe("forward");
    expect(tracker.highWater("namespace:a")).toBeUndefined();
    expect(tracker.highWater("namespace:b")).toEqual({ hlc: "10.0", event_id: "event-b" });
  });
});
