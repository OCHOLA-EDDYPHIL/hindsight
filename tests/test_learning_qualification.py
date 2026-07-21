from __future__ import annotations

import importlib.util
import hashlib
import json
import pathlib
import sys

import pytest
import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"qualification_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _confirmation_corpus() -> dict[str, object]:
    return {
        "variants": [
            {
                "variant_id": f"held-out-{index}",
                "split": "confirmation",
                "recurrence_query": f"private query {index}",
                "reference_lesson": f"private target {index}",
                "context_memories": [
                    {
                        "context_id": f"context-{index}-{candidate}",
                        "role": "hard_distractor",
                        "content": f"private distractor {index} {candidate}",
                    }
                    for candidate in range(3)
                ],
            }
            for index in range(12)
        ]
    }


def _profile() -> dict[str, object]:
    return {
        "id": "profile-digest",
        "provider": "gemini",
        "model": "gemini-embedding-2",
        "dimensions": 1024,
        "capability": "semantic",
        "encoder_revision": "gemini-retrieval-task-v1",
        "configuration": {},
        "max_distance": 0.35,
    }


def test_confirmation_selection_requires_all_frozen_candidate_sets():
    diagnostic = _load_script("run_rank_diagnostics")
    selected = diagnostic._load_variants(mode="confirmation", corpus=_confirmation_corpus())

    assert len(selected) == 12
    assert all(len(row["candidates"]) == 3 for row in selected)

    malformed = _confirmation_corpus()
    malformed["variants"] = malformed["variants"][:-1]
    with pytest.raises(ValueError, match="12 four-candidate"):
        diagnostic._load_variants(mode="confirmation", corpus=malformed)


def test_confirmation_failure_writes_complete_opaque_report(monkeypatch, tmp_path):
    diagnostic = _load_script("run_rank_diagnostics")
    corpus = _confirmation_corpus()
    corpus_path = tmp_path / "frozen.json"
    corpus_path.write_text(json.dumps(corpus), encoding="utf-8")
    output = tmp_path / "qualification.json"

    class Provider:
        provider_name = "gemini"
        model_name = "gemini-embedding-2"
        capability = "semantic"

    monkeypatch.setattr(diagnostic, "DEFAULT_BENCHMARK_CORPUS", corpus_path)
    monkeypatch.setattr(diagnostic, "embedding_provider_from_env", Provider)
    monkeypatch.setattr(diagnostic, "_active_or_empty_profile", lambda **_kwargs: _profile())
    monkeypatch.setattr(
        diagnostic,
        "_benchmark_counts",
        lambda _url: {table: 0 for table in diagnostic.BENCHMARK_TABLES},
    )
    monkeypatch.setattr(
        diagnostic,
        "_diagnose_variant",
        lambda **kwargs: {
            "variant_token": diagnostic.opaque_token("opaque", str(kwargs["row"]["id"])),
            "status": "completed",
            "candidate_count": 4,
            "index_parity": True,
            "direct": {"target_rank_one": False},
            "indexed": {"target_rank_one": False},
        },
    )
    family_sha256 = diagnostic.family_sha256(
        diagnostic.v3_family_contract(
            corpus_sha256=hashlib.sha256(corpus_path.read_bytes()).hexdigest()
        )
    )

    class Tokenizer:
        def __init__(self, *, key_id, family_sha256):
            assert key_id == "alias/test"
            assert len(family_sha256) == 64

        def token(self, *, kind, raw_id):
            return diagnostic.opaque_token("kms-test", kind, raw_id)

    monkeypatch.setattr(diagnostic, "KmsHmacTokenizer", Tokenizer)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_rank_diagnostics.py",
            "confirmation",
            "--database-url",
            "postgresql://root@localhost:26257/hindsight_diagnostic_7_1",
            "--code-sha",
            "a" * 40,
            "--workflow-run-id",
            "7",
            "--workflow-run-attempt",
            "1",
            "--qualification-sequence",
            "1",
            "--scientific-family-sha256",
            family_sha256,
            "--token-key-id",
            "alias/test",
            "--output",
            str(output),
        ],
    )

    with pytest.raises(RuntimeError, match="scientific_failed"):
        diagnostic._main()

    report = json.loads(output.read_text())
    assert report["status"] == "scientific_failed"
    assert report["summary"]["completed_variants"] == 12
    assert report["summary"]["direct_rank_one"] == 0
    assert report["benchmark_state_empty"] is True
    assert report["protocol_identity_sha256"]
    rendered = output.read_text()
    assert "private query" not in rendered
    assert "private target" not in rendered
    assert "private distractor" not in rendered
    assert "held-out-" not in rendered


def test_index_parity_requires_membership_order_and_distance_tolerance():
    diagnostic = _load_script("run_rank_diagnostics")
    direct = {
        "rankings": [
            {"candidate_token": "a", "distance": 0.1},
            {"candidate_token": "b", "distance": 0.2},
        ]
    }
    indexed = {
        "rankings": [
            {"candidate_token": "a", "distance": 0.1000001},
            {"candidate_token": "b", "distance": 0.2000001},
        ]
    }
    assert diagnostic._ordering_parity(direct=direct, indexed=indexed)["index_parity"]
    indexed["rankings"].reverse()
    assert not diagnostic._ordering_parity(direct=direct, indexed=indexed)["index_parity"]


def test_diagnostic_cleanup_refuses_non_workflow_database_names():
    cleanup = _load_script("drop_diagnostic_database")
    name, admin = cleanup.diagnostic_database_target(
        "postgresql://root@db.example:26257/hindsight_diagnostic_123_2?sslmode=require"
    )
    assert name == "hindsight_diagnostic_123_2"
    assert "/defaultdb?" in admin
    with pytest.raises(RuntimeError, match="refusing"):
        cleanup.diagnostic_database_target(
            "postgresql://root@db.example:26257/hindsight?sslmode=require"
        )


def test_interrupted_report_preserves_sequence_and_outcome_boundary(tmp_path):
    authority = _load_script("manage_qualification_family")
    attempt = tmp_path / "attempt.json"
    attempt.write_text(
        json.dumps(
            {
                "code_sha": "a" * 40,
                "sequence": 2,
                "run_id": 7,
                "run_attempt": 1,
            }
        ),
        encoding="utf-8",
    )

    report = authority._infrastructure_report(
        attempt=attempt,
        corpus=ROOT / "fixtures" / "benchmark_variants.json",
        outcome_accessed=True,
    )

    assert report["status"] == "infrastructure_incomplete"
    assert report["workflow"] == {"run_id": 7, "run_attempt": 1, "sequence": 2}
    assert report["outcome_accessed"] is True


def test_qualification_workflow_is_owner_only_outcome_free_and_sealed():
    path = ROOT / ".github" / "workflows" / "learning-qualification.yml"
    workflow = path.read_text()
    parsed = yaml.safe_load(workflow)

    assert set(parsed["jobs"]) == {
        "authorize",
        "exact_main_ci",
        "claim",
        "qualify",
        "qualification_complete",
    }
    assert '"$REF_NAME" == "refs/heads/main"' in workflow
    assert '"$ACTOR" == "$REPOSITORY_OWNER"' in workflow
    assert '"$TRIGGERING_ACTOR" == "$REPOSITORY_OWNER"' in workflow
    assert "verify_ci_provenance.py" in workflow
    assert "manage_qualification_family.py claim" in workflow
    assert "run_rank_diagnostics.py confirmation" in workflow
    assert '--qualification-sequence "$QUALIFICATION_SEQUENCE"' in workflow
    assert '--scientific-family-sha256 "$FAMILY_SHA256"' in workflow
    assert '--token-key-id "$HINDSIGHT_QUALIFICATION_HMAC_KEY_ID"' in workflow
    assert "hindsight_diagnostic_{os.environ['GITHUB_RUN_ID']}_" in workflow
    assert "HINDSIGHT_EVIDENCE_ROLE_ARN" in workflow
    assert "seal_learning_evidence.py" in workflow
    assert "manage_qualification_family.py finalize" in workflow
    assert "drop_diagnostic_database.py" in workflow
    assert workflow.index("manage_qualification_family.py claim") < workflow.index(
        "aws ssm get-parameter"
    )
    for forbidden in (
        "run_learning_benchmark.py",
        "learning-full",
        "configure_changefeed.py pause",
        "protocol_reset",
    ):
        assert forbidden not in workflow
