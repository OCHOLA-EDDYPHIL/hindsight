"""Run the telemetry ingestion demo end to end."""

from __future__ import annotations

import json
from typing import Any

from hindsight.telemetry import run_telemetry_demo


def main() -> None:
    result = run_telemetry_demo()
    print(
        json.dumps(
            {
                "incident_slug": result.ingestion.incident["slug"],
                "namespace": result.ingestion.namespace,
                "memory_id": str(result.ingestion.memory["id"]),
                "agent_thread_id": result.agent_result.thread_id,
                "agent_plan": result.agent_result.plan,
                "proposed_action": result.agent_result.proposed_action,
                "metrics_excerpt": _metrics_excerpt(result.metrics),
                "latest_log": result.logs[-1] if result.logs else None,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


def _metrics_excerpt(metrics: str) -> dict[str, Any]:
    excerpt: dict[str, Any] = {}
    for line in metrics.splitlines():
        if line.startswith("checkout_request_duration_ms_p99"):
            excerpt["checkout_request_duration_ms_p99"] = line
        if line.startswith("payment_processor_timeouts_total"):
            excerpt["payment_processor_timeouts_total"] = line
    return excerpt


if __name__ == "__main__":
    main()
