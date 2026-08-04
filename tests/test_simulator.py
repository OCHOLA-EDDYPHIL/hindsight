"""Tests for the bounded signature action executor."""

import pytest


def test_executor_rejects_empty_and_unknown_action_requests():
    from hindsight.simulator import BoundedActionRequest, DeterministicIncidentSimulator

    tool = DeterministicIncidentSimulator()
    with pytest.raises(ValueError, match="at least one action"):
        tool.execute(BoundedActionRequest(request_id="request-1", actions=()))
    with pytest.raises(ValueError, match="unsupported signature simulator action"):
        tool.execute(BoundedActionRequest(request_id="request-2", actions=("delete_service",)))


def test_executor_returns_observations_and_an_independent_score():
    from hindsight.simulator import BoundedActionRequest, DeterministicIncidentSimulator

    result = DeterministicIncidentSimulator().execute(
        BoundedActionRequest(
            request_id="request-1",
            actions=("inspect_dependency", "throttle_retries"),
        )
    )

    assert result.tool == "deterministic_incident_simulator"
    assert [item["action"] for item in result.observations] == [
        "inspect_dependency",
        "throttle_retries",
    ]
    assert result.recovered is True
    assert result.unsafe_action_count == 0
