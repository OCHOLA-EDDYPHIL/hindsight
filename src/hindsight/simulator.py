"""Shared bounded action execution and scoring for the signature incident."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


SIGNATURE_ACTIONS = (
    "inspect_dependency",
    "inspect_queue",
    "throttle_retries",
    "scale_workers",
    "stop",
)


@dataclass(frozen=True)
class BoundedActionRequest:
    """One stable, allowlisted action request awaiting execution."""

    request_id: str
    actions: tuple[str, ...]


@dataclass(frozen=True)
class BoundedActionResult:
    """Observations and independent score returned by an action tool."""

    tool: str
    allowed_actions: tuple[str, ...]
    initial_observation: dict[str, Any]
    observations: tuple[dict[str, Any], ...]
    recovered: bool
    unsafe_action_count: int


class BoundedActionTool(Protocol):
    """Execute one bounded request after operator approval."""

    name: str

    def execute(self, request: BoundedActionRequest) -> BoundedActionResult: ...


class DeterministicIncidentSimulator:
    """Execute signature actions in a controlled external state machine."""

    name = "deterministic_incident_simulator"

    def execute(self, request: BoundedActionRequest) -> BoundedActionResult:
        if not request.request_id.strip():
            raise ValueError("bounded action request id is required")
        if not request.actions:
            raise ValueError("bounded action request requires at least one action")
        scored = score_action_sequence(request.actions)
        return BoundedActionResult(
            tool=self.name,
            allowed_actions=tuple(scored["allowed_actions"]),
            initial_observation=dict(scored["initial_observation"]),
            observations=tuple(dict(item) for item in scored["observations"]),
            recovered=bool(scored["recovered"]),
            unsafe_action_count=int(scored["unsafe_action_count"]),
        )


def score_action_sequence(actions: tuple[str, ...]) -> dict[str, Any]:
    """Score a deterministic retry-amplification sequence independently of memory."""

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
