"""Shared bounded action scoring over the deterministic incident simulator."""

from __future__ import annotations

from typing import Any

from hindsight.benchmark import IncidentSimulator


def score_action_sequence(
    actions: tuple[str, ...], *, simulator_kind: str = "retry_amplification"
) -> dict[str, Any]:
    """Execute a bounded allowlisted sequence and return raw external observations."""

    simulator = IncidentSimulator(simulator_kind)
    initial = simulator.observe()
    observations = [simulator.step(action) for action in actions]
    return {
        "simulator_kind": simulator_kind,
        "allowed_actions": list(simulator.allowed_actions),
        "requested_actions": list(actions),
        "initial_observation": initial,
        "observations": observations,
        "recovered": simulator.recovered,
        "unsafe_action_count": simulator.unsafe_actions,
    }
