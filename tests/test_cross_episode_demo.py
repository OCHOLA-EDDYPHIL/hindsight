"""Tests for the cross-episode learning demo."""

import os
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


@requires_db
def test_cross_episode_demo_consolidates_and_resolves_repeat_faster():
    from hindsight.cross_episode import run_cross_episode_demo
    from hindsight.db import database_url

    namespace = f"cross-episode-test-{uuid4()}"

    result = run_cross_episode_demo(db_url=database_url(), namespace=namespace)

    assert result.namespace == namespace
    assert result.consolidation.created is True
    assert result.consolidation.memory is not None
    assert result.consolidation.memory["writer"] == "consolidation.worker"
    assert "Consolidated lesson" in result.consolidation.memory["content"]
    assert result.episode_one.steps_to_resolution == 5
    assert result.episode_two.steps_to_resolution == 2
    assert result.steps_saved == 3
    assert result.improvement_ratio == pytest.approx(0.6)
    assert str(result.consolidation.memory["id"]) in result.episode_two.recalled_lesson_memory_ids
    assert "consolidated lesson" in result.episode_two.plan.lower()
