from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from urllib.parse import parse_qs, urlsplit

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


def test_diagnostic_cleanup_normalizes_tls_and_refuses_non_workflow_database_names(
    monkeypatch,
):
    import certifi

    cleanup = _load_script("drop_diagnostic_database")
    database_url = (
        "postgresql://root@db.example:26257/hindsight_diagnostic_123_2"
        "?sslmode=verify-full"
    )
    name, admin = cleanup.diagnostic_database_target(database_url)
    assert name == "hindsight_diagnostic_123_2"
    assert "/defaultdb?" in admin
    connected = []

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, *_args):
            return None

    def fake_connect(url, **kwargs):
        connected.append((url, kwargs))
        return _Connection()

    monkeypatch.setattr(cleanup.psycopg, "connect", fake_connect)
    monkeypatch.setattr(
        "sys.argv",
        ["drop_diagnostic_database.py", "--database-url", database_url],
    )

    cleanup.main()

    query = parse_qs(urlsplit(connected[0][0]).query)
    assert query["sslrootcert"] == [certifi.where()]
    assert connected[0][1] == {"autocommit": True}

    explicit = (
        "postgresql://root@db.example:26257/hindsight_diagnostic_123_2"
        "?sslmode=verify-full&sslrootcert=system"
    )
    _, explicit_admin = cleanup.diagnostic_database_target(explicit)
    assert parse_qs(urlsplit(cleanup.database_url_with_tls_roots(explicit_admin)).query)[
        "sslrootcert"
    ] == ["system"]
    with pytest.raises(RuntimeError, match="refusing"):
        cleanup.diagnostic_database_target(
            "postgresql://root@db.example:26257/hindsight?sslmode=require"
        )


def test_qualification_workflow_is_owner_only_outcome_free_and_sealed():
    path = ROOT / ".github" / "workflows" / "learning-qualification.yml"
    workflow = path.read_text()
    parsed = yaml.safe_load(workflow)

    assert set(parsed["jobs"]) == {
        "authorize",
        "exact_main_ci",
        "qualify",
        "qualification_complete",
    }
    assert '"$REF_NAME" == "refs/heads/main"' in workflow
    assert '"$ACTOR" == "$REPOSITORY_OWNER"' in workflow
    assert '"$TRIGGERING_ACTOR" == "$REPOSITORY_OWNER"' in workflow
    assert "verify_ci_provenance.py" in workflow
    assert "run_rank_diagnostics.py confirmation" in workflow
    assert "hindsight_diagnostic_{os.environ['GITHUB_RUN_ID']}_" in workflow
    assert "HINDSIGHT_EVIDENCE_ROLE_ARN" in workflow
    assert "seal_learning_evidence.py" in workflow
    assert "drop_diagnostic_database.py" in workflow
    for forbidden in (
        "run_learning_benchmark.py",
        "learning-full",
        "configure_changefeed.py pause",
        "protocol_reset",
    ):
        assert forbidden not in workflow
