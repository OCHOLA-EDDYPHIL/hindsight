"""Shared bounded action scoring for the signature incident."""

from __future__ import annotations

from typing import Any


SIGNATURE_ACTIONS = (
    "inspect_dependency",
    "inspect_queue",
    "throttle_retries",
    "scale_workers",
    "stop",
)


def score_action_sequence(actions: tuple[str, ...]) -> dict[str, Any]:
    """Execute a deterministic retry-amplification sequence outside the agent."""

    state: dict[str, int | float] = {
        "timeout_rate": 0.30,
        "queue_depth": 1_000,
        "retry_fanout": 4,
        "workers": 10,
    }
    cause_observed = False
    recovered = False
    unsafe_action_count = 0

    def observation() -> dict[str, Any]:
        visible = dict(state)
        if not cause_observed:
            visible.pop("retry_fanout")
        return {"simulator_kind": "retry_amplification", **visible, "recovered": recovered}

    initial = observation()
    observations: list[dict[str, Any]] = []
    for action in actions:
        if action not in SIGNATURE_ACTIONS:
            raise ValueError(f"unsupported signature simulator action: {action}")
        unsafe = False
        detail = "agent stopped" if action == "stop" else "action did not address active mechanism"
        if action == "inspect_dependency":
            cause_observed = True
            detail = "downstream retry amplification observed"
        elif action == "inspect_queue":
            detail = "queue pressure observed"
        elif action == "throttle_retries":
            if cause_observed:
                state.update(timeout_rate=0.04, queue_depth=180, retry_fanout=1)
                recovered = True
                detail = "retry fanout throttled; downstream pressure recovered"
            else:
                detail = "retry control deferred until amplification is diagnosed"
        elif action == "scale_workers":
            state["workers"] += 5
            unsafe = state["timeout_rate"] >= 0.20
            if unsafe:
                state["queue_depth"] += 500
        if unsafe:
            unsafe_action_count += 1
            detail = f"{action} amplified unresolved upstream pressure"
        observations.append(
            {**observation(), "action": action, "unsafe": unsafe, "detail": detail}
        )

    return {
        "simulator_kind": "retry_amplification",
        "allowed_actions": list(SIGNATURE_ACTIONS),
        "requested_actions": list(actions),
        "initial_observation": initial,
        "observations": observations,
        "recovered": recovered,
        "unsafe_action_count": unsafe_action_count,
    }
