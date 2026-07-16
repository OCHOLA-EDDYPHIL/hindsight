"""Shared local and hosted live-acceptance orchestration checks."""

from __future__ import annotations

import importlib.util
import pathlib
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hindsight_live_acceptance_script",
    ROOT / "scripts" / "run_live_acceptance.py",
)
assert SPEC is not None and SPEC.loader is not None
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


def test_local_pilot_refuses_non_loopback_and_initialized_databases(monkeypatch):
    with pytest.raises(ValueError, match="loopback"):
        acceptance._require_local_database(
            "postgresql://root@example.invalid:26257/pilot?sslmode=disable"
        )

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _query):
            return SimpleNamespace(fetchone=lambda: (True,))

    monkeypatch.setattr(acceptance.psycopg, "connect", lambda _url: Connection())
    with pytest.raises(ValueError, match="fresh database"):
        acceptance._require_local_database(
            "postgresql://root@localhost:26257/pilot?sslmode=disable"
        )


def test_local_pilot_runs_only_fresh_pilot_stages(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEYS", "opaque-material")
    commands = []
    validation = []

    def fake_run(command, *, env, stdout_path=None):
        commands.append((command, env, stdout_path))
        if stdout_path is not None:
            stdout_path.write_text('{"experiment_id":"pilot-id"}')

    monkeypatch.setattr(acceptance, "_run", fake_run)
    monkeypatch.setattr(acceptance, "_require_local_database", lambda _url: None)
    monkeypatch.setattr(acceptance, "_code_sha", lambda: "a" * 40)
    monkeypatch.setattr(
        acceptance,
        "_validate_local_pilot",
        lambda **kwargs: validation.append(kwargs),
    )
    report = tmp_path / "pilot.json"

    acceptance._run_local_pilot(
        SimpleNamespace(
            database_url="postgresql://root@localhost:26257/pilot?sslmode=disable",
            max_distance=0.35,
            report=report,
            code_sha=None,
        )
    )

    flattened = [part for command, _env, _path in commands for part in command]
    assert "scripts/migrate.py" in flattened
    assert "scripts/initialize_agent_storage.py" in flattened
    assert "scripts/reembed_memories.py" in flattened
    assert "pilot" in flattened
    assert "confirmation" not in flattened
    assert "preregister" not in flattened
    assert commands[-1][1]["HINDSIGHT_BENCHMARK_CODE_SHA"] == "a" * 40
    assert validation == [
        {
            "database_url": "postgresql://root@localhost:26257/pilot?sslmode=disable",
            "experiment_id": "pilot-id",
        }
    ]


def test_provider_verification_uses_the_frozen_shared_selectors(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", "opaque-material")
    calls = []
    monkeypatch.setattr(
        acceptance,
        "_run",
        lambda command, *, env, stdout_path=None: calls.append((command, env)),
    )

    acceptance._verify_providers()

    command, env = calls[0]
    assert command[-3:] == list(acceptance.PROVIDER_SELECTORS)
    assert env["RUN_LIVE_GEMINI_EMBEDDINGS"] == "1"
    assert env["RUN_LIVE_GEMINI_REASONING"] == "1"


def test_hosted_benchmark_owns_reports_and_final_gate_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEYS", "opaque-material")
    monkeypatch.setenv("HINDSIGHT_BENCHMARK_CODE_SHA", "b" * 40)
    commands = []

    def fake_run(command, *, env, stdout_path=None):
        commands.append(command)
        if stdout_path is None:
            return
        if "pilot" in command:
            payload = {"experiment_id": "pilot-id"}
        elif "confirmation" in command:
            payload = {
                "experiment_id": "confirmation-id",
                "raw_trace_digest": "trace",
                "claim_authorized": True,
                "gates": {"complete_pairs": True, "retrieval": True},
            }
        else:
            payload = {"pilot_experiment_id": "pilot-id"}
        stdout_path.write_text(acceptance.json.dumps(payload))

    monkeypatch.setattr(acceptance, "_run", fake_run)
    report_dir = tmp_path / "reports"
    summary = tmp_path / "summary.txt"

    acceptance._run_hosted_benchmark(
        SimpleNamespace(
            database_url="postgresql://operator@cluster.example:26257/hindsight?sslmode=verify-full",
            max_distance=0.35,
            report_dir=report_dir,
            summary_path=summary,
        )
    )

    flattened = [part for command in commands for part in command]
    assert flattened.count("pilot") == 1
    assert flattened.count("preregister") == 1
    assert flattened.count("confirmation") == 1
    assert (report_dir / "pilot.json").is_file()
    assert (report_dir / "preregistration.json").is_file()
    assert (report_dir / "confirmation.json").is_file()
    assert "Claim authorized: `True`" in summary.read_text()


def test_confirmation_gate_validation_fails_closed():
    with pytest.raises(RuntimeError, match="did not authorize"):
        acceptance._require_confirmation_gates(
            {"claim_authorized": False, "gates": {"retrieval": True}}
        )
    with pytest.raises(RuntimeError, match="did not authorize"):
        acceptance._require_confirmation_gates(
            {"claim_authorized": True, "gates": {"retrieval": False}}
        )


def test_live_workflow_calls_shared_acceptance_commands():
    workflow = (ROOT / ".github" / "workflows" / "live-acceptance.yml").read_text()

    assert "scripts/run_live_acceptance.py verify-providers" in workflow
    assert "scripts/run_live_acceptance.py hosted-benchmark" in workflow
    assert "run_learning_benchmark.py pilot" not in workflow
    assert "test_live_gemini_embedding_provider_ranks_frozen" not in workflow
