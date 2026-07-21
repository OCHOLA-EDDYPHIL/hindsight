from __future__ import annotations

from types import SimpleNamespace

import pytest

from hindsight.embeddings import GeminiEmbeddingProvider, embedding_profile
from hindsight.representation_selection import select_representation


def _item(*, target_distance: float, competitor_distance: float):
    ranking = [
        {"candidate_token": "target", "distance": target_distance},
        {"candidate_token": "competitor", "distance": competitor_distance},
    ]
    return {
        "item_token": "development-item",
        "target_token": "target",
        "direct": ranking,
        "indexed": [dict(row) for row in ranking],
    }


def _matrix():
    return {
        "schema_version": 1,
        "representations": {
            "raw_control": [_item(target_distance=0.10, competitor_distance=0.125)],
            "generic_title": [_item(target_distance=0.08, competitor_distance=0.12)],
            "applicability_instruction": [_item(target_distance=0.05, competitor_distance=0.11)],
        },
    }


def test_selection_uses_parity_coverage_and_worst_case_margin():
    selected = select_representation(_matrix())

    assert selected["selected_representation"] == "applicability_instruction"
    assert selected["minimum_margin"] == pytest.approx(0.06)
    assert selected["max_distance"] == 0.35
    assert selected["reranking"] is False
    assert selected["fallback"] is False


def test_selection_stops_on_tie_or_raw_control_win():
    tied = _matrix()
    tied["representations"]["generic_title"] = [
        _item(target_distance=0.051, competitor_distance=0.11)
    ]
    with pytest.raises(RuntimeError, match="tie"):
        select_representation(tied)

    raw = _matrix()
    raw["representations"]["raw_control"] = [_item(target_distance=0.01, competitor_distance=0.20)]
    with pytest.raises(RuntimeError, match="raw control won"):
        select_representation(raw)


def test_matrix_schema_rejects_role_or_provenance_metadata():
    report = _matrix()
    report["representations"]["generic_title"][0]["role"] = "target"

    with pytest.raises(ValueError, match="exposes metadata"):
        select_representation(report)


def test_gemini_representations_accept_only_raw_text_and_create_distinct_profiles():
    calls = []

    class Client:
        class Models:
            def embed_content(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.0] * 1024)])

        models = Models()

    class Pool:
        def execute(self, invoke, **_kwargs):
            return SimpleNamespace(value=invoke(Client()))

    raw = GeminiEmbeddingProvider(credential_pool=Pool())
    represented = GeminiEmbeddingProvider(
        credential_pool=Pool(), representation="applicability_instruction"
    )

    raw.embed_query("  incident\r\ntext  ")
    represented.embed_query("  incident\r\ntext  ")
    represented.embed_document("  memory\rtext  ")

    assert calls[0]["contents"] == "incident\ntext"
    assert calls[0]["config"] == {
        "output_dimensionality": 1024,
        "task_type": "RETRIEVAL_QUERY",
    }
    assert calls[1]["contents"].startswith("Retrieve the operational memory")
    assert calls[1]["config"]["task_type"] == "RETRIEVAL_QUERY"
    assert calls[2]["contents"].startswith("Operational memory that may contain")
    assert calls[2]["config"]["title"] == "Hindsight operational memory"
    assert raw.encoder_revision == "gemini-retrieval-task-v1"
    assert represented.encoder_revision.endswith("applicability_instruction")
    assert (
        embedding_profile(raw, max_distance=0.35).profile_id
        != embedding_profile(represented, max_distance=0.35).profile_id
    )
