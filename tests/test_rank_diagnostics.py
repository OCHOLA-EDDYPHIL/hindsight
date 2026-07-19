from __future__ import annotations

import pytest

from hindsight.rank_diagnostics import (
    cosine_distance,
    indexed_candidates,
    opaque_token,
    ranked_candidates,
)


def test_ranked_candidates_reports_only_opaque_ordering_metadata():
    target = opaque_token("variant", "target")
    distractor = opaque_token("variant", "distractor")

    result = ranked_candidates(
        query_embedding=[1.0, 0.0],
        candidates=[
            {"token": target, "role": "target", "embedding": [1.0, 0.0]},
            {
                "token": distractor,
                "role": "hard_distractor",
                "embedding": [0.8, 0.6],
            },
        ],
        target_token=target,
        max_distance=0.35,
    )

    assert result["target_rank"] == 1
    assert result["target_rank_one"] is True
    assert result["target_margin"] == pytest.approx(0.2)
    assert result["rankings"] == [
        {
            "candidate_token": target,
            "candidate_role": "target",
            "distance": 0.0,
            "rank": 1,
        },
        {
            "candidate_token": distractor,
            "candidate_role": "hard_distractor",
            "distance": pytest.approx(0.2),
            "rank": 2,
        },
    ]
    assert "content" not in str(result)
    assert "embedding" not in str(result)


def test_ranked_candidates_records_targets_outside_the_cutoff_without_fallback():
    target = opaque_token("variant", "target")
    result = ranked_candidates(
        query_embedding=[1.0, 0.0],
        candidates=[
            {"token": target, "role": "target", "embedding": [0.0, 1.0]},
        ],
        target_token=target,
        max_distance=0.35,
    )

    assert result == {
        "target_rank": None,
        "target_within_cutoff": False,
        "target_rank_one": False,
        "target_margin": None,
        "rankings": [],
    }


def test_indexed_candidates_rejects_unknown_memories_and_preserves_margin():
    target = opaque_token("variant", "target")
    other = opaque_token("variant", "other")
    hits = [
        {"id": "target-id", "distance": 0.1},
        {"id": "other-id", "distance": 0.3},
    ]

    result = indexed_candidates(
        hits=hits,
        identity_by_memory_id={
            "target-id": (target, "target"),
            "other-id": (other, "background"),
        },
        target_token=target,
    )
    assert result["target_rank"] == 1
    assert result["target_margin"] == pytest.approx(0.2)

    with pytest.raises(ValueError, match="unknown diagnostic memory"):
        indexed_candidates(
            hits=[{"id": "unknown", "distance": 0.0}],
            identity_by_memory_id={},
            target_token=target,
        )


def test_cosine_distance_validates_vector_shape_and_magnitude():
    assert cosine_distance([1.0, 0.0], [1.0, 0.0]) == pytest.approx(0.0)
    assert cosine_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)
    assert cosine_distance([1.0, 1.0], [1.0, 1.0]) == pytest.approx(0.0)
    with pytest.raises(ValueError, match="equal dimensions"):
        cosine_distance([1.0], [1.0, 0.0])
    with pytest.raises(ValueError, match="nonzero magnitude"):
        cosine_distance([0.0, 0.0], [1.0, 0.0])
