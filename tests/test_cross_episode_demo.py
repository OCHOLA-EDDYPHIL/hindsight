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
    assert "consolidated lesson" in result.episode_two.plan.lower()
