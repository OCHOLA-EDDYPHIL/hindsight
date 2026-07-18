import type { Run } from "@/types";

export function formatTime(value?: string | null): string {
  if (!value) return "not recorded";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export function shortId(value?: string | null): string {
  if (!value) return "pending";
  if (value.length <= 16) return value;
  return `${value.slice(0, 8)}…${value.slice(-6)}`;
}

export function humanStatus(value?: string | null): string {
  return value?.replaceAll("_", " ") || "not recorded";
}

export interface StructuredPlan {
  cause: string;
  checks: string;
  action: string;
  safety: string;
}

type PlanSection = "cause" | "checks" | "action";

const SECTION_LABELS: Record<PlanSection, Set<string>> = {
  cause: new Set(["cause", "suspected cause", "likely cause", "root cause"]),
  checks: new Set(["check", "checks", "diagnostic check", "diagnostic checks", "verification"]),
  action: new Set([
    "action",
    "safe action",
    "safe next action",
    "next action",
    "recommended action",
    "remediation",
  ]),
};

function planSectionLabel(value: string): PlanSection | null {
  const normalized = value
    .replace(/^[#*_\s]+|[#*_:\s]+$/g, "")
    .trim()
    .toLocaleLowerCase();
  return (Object.keys(SECTION_LABELS) as PlanSection[]).find((key) =>
    SECTION_LABELS[key].has(normalized),
  ) || null;
}

function sectionMarker(line: string): { section: PlanSection; remainder: string } | null {
  const heading = line.match(/^\s*#{1,6}\s+(.+?)\s*#*\s*$/);
  if (heading) {
    const section = planSectionLabel(heading[1]);
    return section ? { section, remainder: "" } : null;
  }
  const labelled = line.match(/^\s*(?:\*\*|__)?([A-Za-z][A-Za-z ]{0,40}?):(?:\*\*|__)?\s*(.*)$/);
  if (!labelled) return null;
  const section = planSectionLabel(labelled[1]);
  return section ? { section, remainder: labelled[2] } : null;
}

function markdownSections(raw: string): {
  sections: Record<PlanSection, string>;
  preamble: string;
  found: boolean;
} {
  const values: Record<PlanSection, string[]> = { cause: [], checks: [], action: [] };
  const preamble: string[] = [];
  let current: PlanSection | null = null;
  let found = false;
  for (const line of raw.split(/\r?\n/)) {
    const marker = sectionMarker(line);
    if (marker) {
      current = marker.section;
      found = true;
      if (marker.remainder) values[current].push(marker.remainder);
    } else if (current) {
      values[current].push(line);
    } else {
      preamble.push(line);
    }
  }
  return {
    sections: {
      cause: values.cause.join("\n").trim(),
      checks: values.checks.join("\n").trim(),
      action: values.action.join("\n").trim(),
    },
    preamble: preamble.join("\n").trim(),
    found,
  };
}

function plainFragments(raw: string): string[] {
  return raw
    .split(/\n+|;|\.(?:\s+|$)/)
    .map((item) => item.replace(/^\s*(?:[-+*]|\d+[.)])\s+/, "").trim())
    .filter(Boolean);
}

export function structurePlan(run?: Run | null): StructuredPlan {
  const raw = run?.plan?.trim() || "No recommendation was recorded for this decision.";
  const fragments = plainFragments(raw);
  const parsed = markdownSections(raw);
  const proposed = run?.proposed_action?.trim() || "";
  const parsedProposed = markdownSections(proposed);
  const cause = parsed.found
    ? parsed.sections.cause || parsed.preamble || fragments[0] || raw
    : fragments[0] || raw;
  const fallbackChecks = fragments.slice(1, 3);
  const checks =
    parsed.sections.checks ||
    (fallbackChecks.length
      ? fallbackChecks.map((item) => `- ${item}`).join("\n")
      : "- Confirm the cited memory and current incident evidence.");
  const actionCandidate =
    parsedProposed.sections.action ||
    parsed.sections.action ||
    proposed ||
    fragments.at(-1) ||
    raw;
  const action =
    actionCandidate.toLocaleLowerCase() === cause.toLocaleLowerCase()
      ? "Apply this recommendation only after current checks and operator review."
      : actionCandidate;
  return {
    cause,
    checks,
    action,
    safety:
      run?.status === "rejected"
        ? "Rejected. The recommendation remains historical and cannot guide the current state."
        : "Operator approval is required before reflection or memory mutation.",
  };
}

export function isoToLocalInput(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 19);
}

export function localInputToIso(value: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}
