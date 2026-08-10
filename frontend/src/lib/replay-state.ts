import type { InfluenceItem, Run } from "@/types";

export interface ReplayLocation {
  scenarioId: string | null;
  namespace: string;
  asOf: string | null;
}

export function readReplayLocation(
  search: string,
  fallbackNamespace: string,
): ReplayLocation {
  const params = new URLSearchParams(search);
  return {
    scenarioId: params.get("scenario_id") || null,
    namespace: params.get("namespace") || fallbackNamespace,
    asOf: params.get("as_of") || null,
  };
}

function timestamp(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Select the newest recorded run without preferring an older terminal outcome. */
export function latestScenarioRun(runs: Run[]): Run | null {
  return runs.reduce<Run | null>((latest, candidate) => {
    if (!latest) return candidate;
    const latestTime = timestamp(latest.created_at);
    const candidateTime = timestamp(candidate.created_at);
    if (latestTime !== null && candidateTime !== null) {
      return candidateTime >= latestTime ? candidate : latest;
    }
    return candidate;
  }, null);
}

export function influenceFromRun(run: Run | null): InfluenceItem[] {
  return (run?.trace?.reads || []).map((read) => ({
    status: read.memory_status || undefined,
    read: {
      id: read.id,
      rank: read.rank,
      distance: read.distance,
    },
    memory: {
      id: read.memory_id,
      belief_id: read.belief_id,
      version_number: read.version_number,
      status: read.memory_status,
      writer: read.writer,
      source_ref: read.source_ref,
      justification: read.justification,
    },
  }));
}
