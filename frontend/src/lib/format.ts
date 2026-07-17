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
  checks: string[];
  action: string;
  safety: string;
}

export function structurePlan(run?: Run | null): StructuredPlan {
  const raw = run?.plan?.trim() || "No recommendation was recorded for this decision.";
  const fragments = raw
    .split(/\n+|;|\.(?:\s+|$)/)
    .map((item) => item.replace(/^[-*\d.)\s]+/, "").trim())
    .filter(Boolean);
  const cause = fragments[0] || raw;
  const checks = fragments.slice(1, 3);
  const actionCandidate = run?.proposed_action?.trim() || fragments.at(-1) || raw;
  const action =
    actionCandidate.toLocaleLowerCase() === cause.toLocaleLowerCase()
      ? "Apply this recommendation only after current checks and operator review."
      : actionCandidate;
  return {
    cause,
    checks: checks.length ? checks : ["Confirm the cited memory and current incident evidence."],
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
