import type { RealtimeCursor, RealtimeEnvelopeV2, RealtimeEventType } from "@/types";

const DEFAULT_EVENT_LIMIT = 1024;
const DEFAULT_STREAM_LIMIT = 32;
const EVENT_TYPES = new Set<RealtimeEventType>(["memory", "operation", "run", "run_event"]);
const HLC_PATTERN = /^(\d+)(?:\.(\d+))?$/;

export type RealtimeEventDisposition = "forward" | "duplicate" | "reordered";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`${field} must be a non-empty string`);
  }
  return value;
}

function optionalString(value: unknown, field: string): string | null | undefined {
  if (value === undefined || value === null) return value;
  if (typeof value !== "string") throw new Error(`${field} must be a string or null`);
  return value;
}

function parseHlc(value: string): [bigint, bigint] {
  const match = HLC_PATTERN.exec(value);
  if (!match) throw new Error("realtime cursor HLC is invalid");
  return [BigInt(match[1]), BigInt(match[2] || "0")];
}

export function compareRealtimeCursors(left: RealtimeCursor, right: RealtimeCursor): number {
  const [leftWallTime, leftLogical] = parseHlc(left.hlc);
  const [rightWallTime, rightLogical] = parseHlc(right.hlc);
  if (leftWallTime !== rightWallTime) return leftWallTime < rightWallTime ? -1 : 1;
  if (leftLogical !== rightLogical) return leftLogical < rightLogical ? -1 : 1;
  return left.event_id < right.event_id ? -1 : left.event_id > right.event_id ? 1 : 0;
}

export function parseRealtimeEnvelopeV2(value: unknown): RealtimeEnvelopeV2 | null {
  if (!isRecord(value) || value.version !== 2) return null;
  const eventId = requiredString(value.event_id, "realtime event_id");
  if (!isRecord(value.cursor)) throw new Error("realtime cursor is required");
  const cursor = {
    hlc: requiredString(value.cursor.hlc, "realtime cursor HLC"),
    event_id: requiredString(value.cursor.event_id, "realtime cursor event_id"),
  };
  parseHlc(cursor.hlc);
  if (cursor.event_id !== eventId) {
    throw new Error("realtime cursor event_id must match the envelope event_id");
  }
  if (typeof value.type !== "string" || !EVENT_TYPES.has(value.type as RealtimeEventType)) {
    throw new Error("realtime event type is invalid");
  }
  if (!isRecord(value.data)) throw new Error("realtime event data must be an object");
  return {
    version: 2,
    event_id: eventId,
    cursor,
    type: value.type as RealtimeEventType,
    namespace: optionalString(value.namespace, "realtime namespace"),
    run_id: optionalString(value.run_id, "realtime run_id"),
    data: value.data,
  };
}

export class BoundedRealtimeTracker {
  private readonly recentEventIds = new Map<string, true>();
  private readonly highWaterByStream = new Map<string, RealtimeCursor>();

  constructor(
    private readonly eventLimit = DEFAULT_EVENT_LIMIT,
    private readonly streamLimit = DEFAULT_STREAM_LIMIT,
  ) {
    if (!Number.isInteger(eventLimit) || eventLimit < 1) {
      throw new Error("realtime event limit must be a positive integer");
    }
    if (!Number.isInteger(streamLimit) || streamLimit < 1) {
      throw new Error("realtime stream limit must be a positive integer");
    }
  }

  observe(stream: string, envelope: RealtimeEnvelopeV2): RealtimeEventDisposition {
    if (this.recentEventIds.has(envelope.event_id)) return "duplicate";
    this.rememberEvent(envelope.event_id);

    const highWater = this.highWaterByStream.get(stream);
    if (!highWater || compareRealtimeCursors(envelope.cursor, highWater) > 0) {
      this.rememberHighWater(stream, envelope.cursor);
      return "forward";
    }
    this.touchStream(stream, highWater);
    return "reordered";
  }

  highWater(stream: string): RealtimeCursor | undefined {
    return this.highWaterByStream.get(stream);
  }

  private rememberEvent(eventId: string) {
    this.recentEventIds.set(eventId, true);
    while (this.recentEventIds.size > this.eventLimit) {
      const oldest = this.recentEventIds.keys().next().value;
      if (oldest === undefined) break;
      this.recentEventIds.delete(oldest);
    }
  }

  private rememberHighWater(stream: string, cursor: RealtimeCursor) {
    this.highWaterByStream.delete(stream);
    this.highWaterByStream.set(stream, cursor);
    while (this.highWaterByStream.size > this.streamLimit) {
      const oldest = this.highWaterByStream.keys().next().value;
      if (oldest === undefined) break;
      this.highWaterByStream.delete(oldest);
    }
  }

  private touchStream(stream: string, cursor: RealtimeCursor) {
    this.highWaterByStream.delete(stream);
    this.highWaterByStream.set(stream, cursor);
  }
}
