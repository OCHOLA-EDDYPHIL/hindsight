from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from hindsight.embeddings import GeminiEmbeddingProvider, embedding_profile
from hindsight.rank_diagnostics import cosine_distance
from hindsight.representation_selection import build_representation_matrix, select_representation


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
        "schema_version": 2,
        "development_sha256": "d" * 64,
        "representations": {
            "raw_control": [_item(target_distance=0.10, competitor_distance=0.125)],
            "generic_title": [_item(target_distance=0.08, competitor_distance=0.12)],
            "applicability_instruction": [_item(target_distance=0.05, competitor_distance=0.11)],
        },
        "embedding_profiles": {
            name: _profile(name)
            for name in ("raw_control", "generic_title", "applicability_instruction")
        },
    }


def _profile(representation: str):
    return {
        "profile_id": f"profile-{representation}",
        "provider": "gemini",
        "model": "gemini-embedding-2",
        "dimensions": 1024,
        "capability": "semantic",
        "encoder_revision": f"revision-{representation}",
        "configuration": {},
        "max_distance": 0.35,
        "representation": representation,
    }


def test_selection_uses_parity_coverage_and_worst_case_margin():
    selected = select_representation(_matrix())

    assert selected["selected_representation"] == "applicability_instruction"
    assert selected["minimum_margin"] == pytest.approx(0.06)
    assert selected["max_distance"] == 0.35
    assert selected["reranking"] is False
    assert selected["fallback"] is False
    assert selected["embedding_profile"]["representation"] == "applicability_instruction"
    assert len(selected["representation_matrix_sha256"]) == 64


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


def test_matrix_builder_accepts_only_twelve_development_items():
    variants = [{"variant_id": f"item-{index}"} for index in range(12)]
    evaluations = {
        name: [
            {
                **_item(target_distance=0.05, competitor_distance=0.11),
                "item_token": row["variant_id"],
            }
            for row in variants
        ]
        for name in ("raw_control", "generic_title", "applicability_instruction")
    }
    profiles = {name: _profile(name) for name in evaluations}

    matrix = build_representation_matrix(
        development_package={"schema_version": 4, "split": "development", "variants": variants},
        evaluations=evaluations,
        embedding_profiles=profiles,
    )

    assert matrix["schema_version"] == 2
    assert len(matrix["development_sha256"]) == 64

    with pytest.raises(ValueError, match="development split"):
        build_representation_matrix(
            development_package={"schema_version": 4, "split": "pilot", "variants": variants},
            evaluations=evaluations,
            embedding_profiles=profiles,
        )


def test_matrix_generator_compares_direct_and_indexed_rankings_without_roles(monkeypatch):
    path = (
        Path(__file__).resolve().parents[1] / "scripts" / "build_embedding_representation_matrix.py"
    )
    spec = importlib.util.spec_from_file_location("build_embedding_representation_matrix", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Provider:
        def embed_query(self, _text):
            return [1.0, 0.0]

        def embed_document(self, text):
            if text == "target":
                return [1.0, 0.0]
            return [0.98, 0.1 + (int(text.rsplit("-", 1)[1]) * 0.05)]

    class Store:
        def __init__(self, **_kwargs):
            self.rows = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def write_semantic(self, *, precomputed_embedding, **_kwargs):
            memory_id = f"memory-{len(self.rows)}"
            self.rows.append((memory_id, precomputed_embedding))
            return {"id": memory_id}

        def search_semantic_vector(self, *, query_vector, **_kwargs):
            return [
                {"id": memory_id, "distance": cosine_distance(query_vector, embedding)}
                for memory_id, embedding in sorted(
                    self.rows,
                    key=lambda row: cosine_distance(query_vector, row[1]),
                )
            ]

    monkeypatch.setattr(module, "begin_profile_build", lambda **_kwargs: {"id": "profile"})
    monkeypatch.setattr(module, "activate_profile", lambda **_kwargs: None)
    monkeypatch.setattr(module, "MemoryStore", Store)
    package = {
        "schema_version": 4,
        "split": "development",
        "variants": [
            {
                "variant_id": "development-item",
                "recurrence_query": "query",
                "reference_lesson": "target",
                "context_memories": [
                    {
                        "context_id": f"context-{index}",
                        "role": "background",
                        "content": f"other-{index}",
                    }
                    for index in range(3)
                ],
            }
        ],
    }

    rows = module._evaluate_representation(
        package=package,
        representation="raw_control",
        provider=Provider(),
        db_url="postgresql://unused",
    )

    assert set(rows[0]) == {"item_token", "target_token", "direct", "indexed"}
    assert rows[0]["direct"] == rows[0]["indexed"]
    assert all(set(row) == {"candidate_token", "distance"} for row in rows[0]["direct"])
    module._require_disposable_database(
        "postgresql://root@localhost/hindsight_representation_raw_control",
        representation="raw_control",
    )
    with pytest.raises(RuntimeError, match="disposable database"):
        module._require_disposable_database(
            "postgresql://root@localhost/defaultdb",
            representation="raw_control",
        )


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
