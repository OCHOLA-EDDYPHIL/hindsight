"""Tests for the cross-episode mechanism demo."""

from dataclasses import asdict, fields
import os
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


def test_cross_episode_output_schema_contains_no_performance_metrics():
    from hindsight.cross_episode import CrossEpisodeRunSummary

    field_names = {field.name for field in fields(CrossEpisodeRunSummary)}

    assert field_names.isdisjoint(
        {
            "elapsed_ms",
            "duration_ms",
            "steps_saved",
            "improvement",
            "improvement_percentage",
        }
    )


@requires_db
def test_cross_episode_demo_shows_lesson_recall_without_performance_fields():
    from hindsight.cross_episode import run_cross_episode_demo
    from hindsight.db import database_url

    namespace = f"cross-episode-test-{uuid4()}"

    result = run_cross_episode_demo(db_url=database_url(), namespace=namespace)

    assert result.namespace.startswith(f"{namespace}:session:")
    assert result.consolidation.created is True
    assert result.consolidation.memory is not None
    assert result.consolidation.memory["writer"] == "consolidation.worker"
    assert result.consolidation.memory["content_schema"] == "procedural_lesson.v1"
    payload = asdict(result)
    assert "elapsed_ms" not in str(payload)
    assert "steps_saved" not in str(payload)
    assert "improvement_percentage" not in str(payload)
    assert str(result.consolidation.memory["id"]) in result.episode_two.recalled_lesson_memory_ids
    assert result.episode_two.retrieval_id
    lesson_trace = next(
        trace
        for trace in result.episode_two.recalled_memory_traces
        if str(trace["memory_id"]) == str(result.consolidation.memory["id"])
    )
    assert str(lesson_trace["retrieval_id"]) == result.episode_two.retrieval_id
    assert lesson_trace["belief_id"] == result.consolidation.memory["belief_id"]
    assert lesson_trace["version_number"] == result.consolidation.memory["version_number"]
    assert lesson_trace["embedding_profile_id"]
    assert lesson_trace["memory_producer_decision_id"] == result.consolidation.memory[
        "producer_decision_id"
    ]
    assert lesson_trace["incoming_lineage_edge_ids"]
    assert "consolidated lesson" in result.episode_two.plan.lower()

    identity = result.lesson_trace
    assert str(identity["source_incident"]["id"]) == str(result.consolidation.incident["id"])
    assert str(identity["consolidation"]["job_id"]) == result.consolidation.job_id
    assert identity["consolidation"]["producer_decision_id"] == result.consolidation.memory[
        "producer_decision_id"
    ]
    assert identity["lesson"] == {
        "memory_id": result.consolidation.memory["id"],
        "belief_id": result.consolidation.memory["belief_id"],
        "version_number": result.consolidation.memory["version_number"],
        "embedding_profile_id": identity["retrieval"]["embedding_profile_id"],
    }
    assert str(identity["retrieval"]["retrieval_id"]) == result.episode_two.retrieval_id
    assert identity["embedding_profile"]["id"] == identity["lesson"][
        "embedding_profile_id"
    ]
    assert identity["lineage_edges"]
    assert identity["consumer_decision"]["decision_id"] == result.episode_two.decision_id
    def field_names(value):
        if isinstance(value, dict):
            return set(value).union(*(field_names(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(field_names(item) for item in value))
        return set()

    identity_fields = field_names(identity)
    for excluded in ("content", "plan", "prompt", "elapsed", "duration", "improvement"):
        assert excluded not in identity_fields

    from fastapi.testclient import TestClient
    from hindsight.api import app

    client = TestClient(app)
    by_decision = client.get(f"/v1/lesson-traces/{result.episode_two.decision_id}")
    assert by_decision.status_code == 200
    assert by_decision.json()["lesson"]["memory_id"] == str(result.consolidation.memory["id"])
    recent = client.get("/v1/lesson-traces", params={"limit": 1})
    assert recent.status_code == 200
    assert recent.json()["count"] == 1
    assert recent.json()["traces"][0]["consumer_decision"]["decision_id"] == (
        result.episode_two.decision_id
    )
