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


def test_local_acceptance_refuses_non_loopback_and_existing_databases(monkeypatch):
    with pytest.raises(ValueError, match="loopback"):
        acceptance._validate_local_url(
            "postgresql://root@example.invalid:26257/pilot?sslmode=disable"
        )

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _query, _params=None):
            return SimpleNamespace(fetchone=lambda: (1,))

    monkeypatch.setattr(acceptance.psycopg, "connect", lambda *_args, **_kwargs: Connection())
    with pytest.raises(ValueError, match="new database"):
        acceptance._create_local_database(
            "postgresql://root@localhost:26257/pilot?sslmode=disable"
        )
    with pytest.raises(ValueError, match="outside the repository"):
        acceptance._require_local_report_path(ROOT / "acceptance-reports")


def test_product_provider_verification_excludes_frozen_pilot(monkeypatch):
    monkeypatch.setenv(
        "GEMINI_API_KEYS",
        '{"version":1,"keys":[{"id":"one","api_key":"opaque-material"}]}',
    )
    calls = []
    artifact_dir = pathlib.Path("/tmp/providers")
    monkeypatch.setattr(acceptance, "_acceptance_artifact_dir", lambda _phase: artifact_dir)
    monkeypatch.setattr(
        acceptance,
        "_run_strict_pytest",
        lambda selectors, *, env, phase, artifact_dir: calls.append(
            (selectors, env, phase, artifact_dir)
        ),
    )

    acceptance._verify_product_providers()

    assert len(calls) == 1
    assert calls[0][0] == acceptance.PROVIDER_SANITY_SELECTORS
    assert acceptance.PILOT_EMBEDDING_SELECTOR not in calls[0][0]
    assert calls[0][1]["RUN_LIVE_GEMINI_EMBEDDINGS"] == "1"
    assert calls[0][1]["GEMINI_API_KEY"] == "opaque-material"
    assert calls[0][2:] == ("providers", artifact_dir)


def test_learning_provider_verification_runs_frozen_pilot_twice(monkeypatch):
    monkeypatch.setenv(
        "GEMINI_API_KEYS",
        '{"version":1,"keys":[{"id":"one","api_key":"opaque-material"}]}',
    )
    calls = []
    monkeypatch.setattr(
        acceptance,
        "_run",
        lambda command, *, env, stdout_path=None: calls.append((command, env)),
    )

    acceptance._verify_learning_providers()

    assert [call[0][-1] for call in calls] == [
        acceptance.PILOT_EMBEDDING_SELECTOR,
        acceptance.PILOT_EMBEDDING_SELECTOR,
    ]
    assert all(call[1]["RUN_LIVE_GEMINI_EMBEDDINGS"] == "1" for call in calls)
    assert all(call[1]["GEMINI_API_KEY"] == "opaque-material" for call in calls)


def test_semantic_verification_uses_shared_live_selectors_and_explicit_scope(monkeypatch):
    database_url = "postgresql://root@localhost:26257/db?sslmode=disable"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("GEMINI_API_KEYS", "opaque-material")
    calls = []
    monkeypatch.setattr(
        acceptance,
        "_acceptance_artifact_dir",
        lambda phase: pathlib.Path("/tmp") / phase,
    )
    monkeypatch.setattr(
        acceptance,
        "_run_strict_pytest",
        lambda selectors, *, env, phase, artifact_dir: calls.append(
            (selectors, env, phase, artifact_dir)
        ),
    )

    acceptance._verify_semantic(
        SimpleNamespace(
            database_url=database_url,
            database_scope="local",
        )
    )

    assert calls[0][0] == acceptance.LOCAL_SEMANTIC_SELECTORS
    assert calls[0][1]["RUN_LIVE_GEMINI_ACCEPTANCE"] == "1"
    with pytest.raises(ValueError, match="refuses loopback"):
        acceptance._verify_semantic(
            SimpleNamespace(
                database_url=database_url,
                database_scope="hosted",
            )
        )

    acceptance._verify_semantic(
        SimpleNamespace(
            database_url=(
                "postgresql://operator@cluster.example:26257/hindsight?sslmode=verify-full"
            ),
            database_scope="hosted",
        )
    )

    assert calls[1][0] == acceptance.SEMANTIC_RETRIEVAL_SELECTORS
    assert acceptance.DIRECT_CONSOLIDATION_SELECTOR not in calls[1][0]


def test_hosted_product_phases_are_selector_isolated():
    phases = acceptance.HOSTED_PHASE_SELECTORS

    assert set(phases) == {"semantic", "consolidation", "worker", "browser", "roles"}
    assert phases["semantic"] == acceptance.SEMANTIC_RETRIEVAL_SELECTORS
    assert (
        phases["consolidation"]
        == acceptance.HOSTED_ONLY_INFRASTRUCTURE_SELECTORS_BY_PHASE["consolidation"]
    )
    assert phases["browser"] == acceptance.HOSTED_BROWSER_PRODUCT_SELECTORS
    assert acceptance.DIRECT_CONSOLIDATION_SELECTOR not in {
        selector for selectors in phases.values() for selector in selectors
    }
    flattened = [selector for selectors in phases.values() for selector in selectors]
    assert len(flattened) == len(set(flattened))


@pytest.mark.parametrize(
    ("phase", "expected_tenant"),
    (
        ("semantic", acceptance.ACCEPTANCE_TENANT_ID),
        ("worker", acceptance.PUBLIC_DEMO_TENANT_ID),
        ("browser", acceptance.PUBLIC_DEMO_TENANT_ID),
    ),
)
def test_hosted_product_binds_server_owned_phase_tenant(
    monkeypatch, phase, expected_tenant
):
    database_url = (
        "postgresql://runtime@cluster.example:26257/hindsight?sslmode=verify-full"
    )
    monkeypatch.setenv("PGOPTIONS", "-c hindsight.tenant_id=untrusted")
    monkeypatch.setattr(acceptance, "_require_gemini_credentials", lambda: None)
    monkeypatch.setattr(acceptance, "_verify_hosted_endpoints", lambda: None)
    monkeypatch.setattr(acceptance, "_verify_changefeed", lambda _env: None)
    monkeypatch.setattr(acceptance, "_required_env", lambda _name: "configured")
    monkeypatch.setattr(acceptance, "_required_positive_int_env", lambda _name: 1)
    calls = []
    monkeypatch.setattr(
        acceptance,
        "_run_hosted_pytest",
        lambda selectors, *, env, phase: calls.append((selectors, env, phase)),
    )

    acceptance._run_hosted_product(
        SimpleNamespace(phase=phase, database_url=database_url)
    )

    selectors, env, selected_phase = calls[0]
    assert selectors == acceptance.HOSTED_PHASE_SELECTORS[phase]
    assert selected_phase == phase
    assert env["PGOPTIONS"] == f"-c hindsight.tenant_id={expected_tenant}"


def test_browser_contract_inventories_have_explicit_local_hosted_parity():
    expected_shared = (
        "tests/test_browser_ui.py::test_operator_can_run_and_explain_signature_workflow",
        "tests/test_browser_ui.py::"
        "test_cross_episode_lesson_identity_chain_is_inspectable",
        "tests/test_browser_ui.py::"
        "test_review_required_memory_renders_as_active_in_its_historical_snapshot",
    )
    expected_infrastructure = (
        "tests/test_hosted_acceptance.py::"
        "test_resolved_transition_reaches_managed_changefeed_worker_and_cited_lesson",
        "tests/test_hosted_acceptance.py::"
        "test_websocket_requires_resubscribe_after_reconnect_and_honors_unsubscribe",
    )

    assert acceptance.SHARED_BROWSER_CONTRACT_SELECTORS == expected_shared
    assert acceptance.HOSTED_ONLY_INFRASTRUCTURE_SELECTORS == expected_infrastructure
    assert set(expected_shared).issubset(acceptance.LOCAL_BROWSER_PRODUCT_SELECTORS)
    assert set(expected_shared).issubset(acceptance.HOSTED_BROWSER_PRODUCT_SELECTORS)
    assert set(expected_shared).isdisjoint(expected_infrastructure)
    assert (
        set(acceptance.HOSTED_BROWSER_PRODUCT_SELECTORS)
        - set(acceptance.LOCAL_BROWSER_PRODUCT_SELECTORS)
        == set(acceptance.HOSTED_ONLY_INFRASTRUCTURE_SELECTORS_BY_PHASE["browser"])
    )
    assert (
        set(acceptance.HOSTED_BROWSER_PRODUCT_SELECTORS)
        - set(acceptance.HOSTED_ONLY_INFRASTRUCTURE_SELECTORS_BY_PHASE["browser"])
        == set(expected_shared)
    )


def test_hosted_pytest_rejects_skipped_gates(monkeypatch, tmp_path):
    def fake_run(command, **_kwargs):
        report = pathlib.Path(next(part.split("=", 1)[1] for part in command if part.startswith("--junitxml=")))
        report.write_text('<testsuites><testsuite tests="1" skipped="1"/></testsuites>')
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv(acceptance.ACCEPTANCE_ARTIFACT_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(acceptance.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="1 skipped"):
        acceptance._run_hosted_pytest(
            ("tests/test_example.py::test_gate",), env={}, phase="semantic"
        )


def test_local_browser_rejects_skipped_shared_contract(monkeypatch, tmp_path):
    def fake_run(command, **_kwargs):
        report = pathlib.Path(
            next(
                part.split("=", 1)[1]
                for part in command
                if part.startswith("--junitxml=")
            )
        )
        report.write_text(
            '<testsuites><testsuite tests="2" skipped="1"/></testsuites>'
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(acceptance.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="2 tests with 1 skipped"):
        acceptance._run_strict_pytest(
            acceptance.SHARED_BROWSER_CONTRACT_SELECTORS,
            env={},
            phase="local-browser",
            artifact_dir=tmp_path,
        )


def _completed_report(kind: str, experiment_id: str) -> dict[str, object]:
    return {
        "experiment_id": experiment_id,
        "experiment_kind": kind,
        "status": "completed",
        "raw_trace_digest": "trace",
        "claim_evidence_digest": "claim",
    }


def test_local_pilot_runs_setup_and_preregisters_without_confirmation(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEYS", "opaque-material")
    commands = []
    validations = []

    def fake_run(command, *, env, stdout_path=None):
        commands.append((command, env, stdout_path))
        if stdout_path is None:
            return
        if "pilot" in command:
            payload = _completed_report("pilot", "pilot-id")
        else:
            payload = {
                "pilot_experiment_id": "pilot-id",
                "preregistration_sha256": "digest",
                "eligible_held_out_variant_ids": [f"eligible-{i}" for i in range(12)],
                "selected_held_out_variant_ids": [f"selected-{i}" for i in range(12)],
                "repetitions_per_variant": 2,
            }
        stdout_path.write_text(acceptance.json.dumps(payload))

    monkeypatch.setattr(acceptance, "_run", fake_run)
    monkeypatch.setattr(acceptance, "_create_local_database", lambda _url: None)
    monkeypatch.setattr(acceptance, "_require_local_code_sha", lambda _sha: "a" * 40)
    monkeypatch.setattr(
        acceptance,
        "_validate_experiment",
        lambda **kwargs: validations.append(kwargs),
    )
    report_dir = tmp_path / "reports"

    acceptance._run_local_benchmark(
        SimpleNamespace(
            database_url="postgresql://root@localhost:26257/pilot?sslmode=disable",
            max_distance=0.35,
            report_dir=report_dir,
            code_sha=None,
        ),
        include_confirmation=False,
    )

    flattened = [part for command, _env, _path in commands for part in command]
    assert "scripts/migrate.py" in flattened
    assert "scripts/initialize_agent_storage.py" in flattened
    assert "scripts/reembed_memories.py" in flattened
    assert "pilot" in flattened
    assert "confirmation" not in flattened
    assert "preregister" in flattened
    assert validations[0]["expected_trials"] == 36
    assert (report_dir / "pilot.json").is_file()
    assert (report_dir / "preregistration.json").is_file()


def test_local_benchmark_checks_clean_sha_before_creating_database(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEYS", "opaque-material")
    order = []
    monkeypatch.setattr(
        acceptance,
        "_require_local_code_sha",
        lambda _sha: order.append("sha") or "a" * 40,
    )
    monkeypatch.setattr(
        acceptance,
        "_create_local_database",
        lambda _url: order.append("database"),
    )
    monkeypatch.setattr(
        acceptance,
        "_run_pilot_and_preregister",
        lambda **_kwargs: ({}, {}),
    )
    monkeypatch.setattr(acceptance, "_run", lambda *_args, **_kwargs: None)

    acceptance._run_local_benchmark(
        SimpleNamespace(
            database_url="postgresql://root@localhost:26257/pilot?sslmode=disable",
            max_distance=0.35,
            report_dir=tmp_path / "reports",
            code_sha=None,
        ),
        include_confirmation=False,
    )

    assert order == ["sha", "database"]


def test_local_browser_product_uses_live_handler_and_runs_history(monkeypatch, tmp_path):
    monkeypatch.setenv("RUN_HOSTED_ACCEPTANCE", "1")
    monkeypatch.setenv("HOSTED_API_URL", "https://hosted.invalid")
    monkeypatch.setenv("GEMINI_API_KEYS", "opaque-material")
    calls = []

    class Server:
        def terminate(self):
            return None

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(acceptance.subprocess, "Popen", lambda *_args, **_kwargs: Server())
    monkeypatch.setattr(acceptance, "_wait_for_http_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(acceptance, "_acceptance_artifact_dir", lambda _phase: tmp_path)
    monkeypatch.setattr(
        acceptance,
        "_run_strict_pytest",
        lambda selectors, *, env, phase, artifact_dir: calls.append(
            (selectors, env, phase, artifact_dir)
        ),
    )

    acceptance._run_local_browser_product(
        database_url="postgresql://root@localhost:26257/product?sslmode=disable",
        base_url="http://127.0.0.1:8766",
    )

    selectors, env, phase, artifact_dir = calls[0]
    assert "RUN_HOSTED_ACCEPTANCE" not in env
    assert "HOSTED_API_URL" not in env
    assert env["EMBEDDING_PROVIDER"] == "gemini"
    assert env["LLM_PROVIDER"] == "gemini"
    assert env["HINDSIGHT_INLINE_WORKER"] == "1"
    assert env["HINDSIGHT_ALLOWED_ORIGINS"] == "http://127.0.0.1:8766"
    assert selectors == acceptance.LOCAL_BROWSER_PRODUCT_SELECTORS
    assert set(acceptance.SHARED_BROWSER_CONTRACT_SELECTORS).issubset(selectors)
    assert phase == "local-browser"
    assert artifact_dir == tmp_path


def test_local_product_full_uses_fresh_stage_databases_without_sha_gate(monkeypatch):
    created = []
    initialized = []
    calls = []
    monkeypatch.setattr(
        acceptance,
        "_create_local_database",
        lambda database_url: created.append(database_url),
    )
    monkeypatch.setattr(acceptance, "_verify_product_providers", lambda: calls.append("providers"))
    monkeypatch.setattr(
        acceptance,
        "_initialize_product_database",
        lambda env, *, configure_embeddings: initialized.append(
            (env["DATABASE_URL"], configure_embeddings)
        ),
    )
    monkeypatch.setattr(acceptance, "_verify_semantic", lambda _args: calls.append("semantic"))
    monkeypatch.setattr(
        acceptance,
        "_verify_resilience",
        lambda _args: calls.append("resilience"),
    )
    monkeypatch.setattr(
        acceptance,
        "_run_local_browser_product",
        lambda **_kwargs: calls.append("browser"),
    )
    monkeypatch.setattr(
        acceptance,
        "_require_local_code_sha",
        lambda _value: pytest.fail("product acceptance must not require an exact SHA"),
    )
    monkeypatch.setattr(
        acceptance,
        "uuid4",
        lambda: SimpleNamespace(hex="1234567890abcdef1234567890abcdef"),
    )

    acceptance._run_local_product_full(
        SimpleNamespace(
            database_url="postgresql://root@localhost:26257/product?sslmode=disable",
            base_url="http://127.0.0.1:8766",
        )
    )

    assert [parts.path for parts in map(acceptance.urlsplit, created)] == [
        "/product_1234567890ab_semantic",
        "/product_1234567890ab_resilience",
        "/product_1234567890ab_browser",
    ]
    assert [configured for _url, configured in initialized] == [True, False, True]
    assert calls == ["providers", "semantic", "resilience", "browser"]


def test_full_benchmark_owns_reports_and_strict_validations(monkeypatch, tmp_path):
    commands = []
    validations = []

    def fake_run(command, *, env, stdout_path=None):
        commands.append(command)
        if stdout_path is None:
            return
        if "pilot" in command:
            payload = _completed_report("pilot", "pilot-id")
        elif "confirmation" in command:
            payload = {
                **_completed_report("confirmation", "confirmation-id"),
                "claim_authorized": True,
                "gates": {"complete_pairs": True, "retrieval": True},
            }
        else:
            payload = {
                "pilot_experiment_id": "pilot-id",
                "preregistration_sha256": "digest",
                "eligible_held_out_variant_ids": [f"eligible-{i}" for i in range(12)],
                "selected_held_out_variant_ids": [f"selected-{i}" for i in range(12)],
                "repetitions_per_variant": 2,
            }
        stdout_path.write_text(acceptance.json.dumps(payload))

    monkeypatch.setattr(acceptance, "_run", fake_run)
    monkeypatch.setattr(
        acceptance,
        "_validate_experiment",
        lambda **kwargs: validations.append(kwargs),
    )
    report_dir = tmp_path / "reports"
    report_dir.mkdir()

    confirmation = acceptance._run_benchmark_sequence(
        database_url="postgresql://root@localhost/db",
        env={},
        report_dir=report_dir,
    )

    flattened = [part for command in commands for part in command]
    assert flattened.count("pilot") == 1
    assert flattened.count("preregister") == 1
    assert flattened.count("confirmation") == 1
    assert [item["expected_trials"] for item in validations] == [36, 72]
    assert confirmation["claim_authorized"] is True
    assert all((report_dir / name).stat().st_size > 0 for name in (
        "pilot.json",
        "preregistration.json",
        "confirmation.json",
    ))


def test_learning_failure_is_strict_and_preserves_reports(monkeypatch, tmp_path):
    def fake_run(command, *, env, stdout_path=None):
        if stdout_path is None:
            return
        if "pilot" in command:
            payload = _completed_report("pilot", "pilot-id")
        elif "confirmation" in command:
            payload = {
                **_completed_report("confirmation", "confirmation-id"),
                "claim_authorized": False,
                "gates": {"complete_pairs": True, "retrieval": False},
            }
        else:
            payload = {
                "pilot_experiment_id": "pilot-id",
                "preregistration_sha256": "digest",
                "eligible_held_out_variant_ids": [f"eligible-{i}" for i in range(12)],
                "selected_held_out_variant_ids": [f"selected-{i}" for i in range(12)],
                "repetitions_per_variant": 2,
            }
        stdout_path.write_text(acceptance.json.dumps(payload))

    monkeypatch.setattr(acceptance, "_run", fake_run)
    monkeypatch.setattr(acceptance, "_validate_experiment", lambda **_kwargs: None)
    report_dir = tmp_path / "reports"
    report_dir.mkdir()

    with pytest.raises(RuntimeError, match="did not authorize"):
        acceptance._run_benchmark_sequence(
            database_url="postgresql://root@localhost/db",
            env={},
            report_dir=report_dir,
        )

    assert all(
        (report_dir / name).stat().st_size > 0
        for name in ("pilot.json", "preregistration.json", "confirmation.json")
    )


def test_experiment_validation_requires_every_trial(monkeypatch):
    rows = iter(
        [
            ("pilot", "completed"),
            (6, 6),
            (36, 36, 36, 0, 0),
        ]
    )

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _query, _params):
            return SimpleNamespace(fetchone=lambda: next(rows))

    monkeypatch.setattr(acceptance.psycopg, "connect", lambda _url: Connection())
    acceptance._validate_experiment(
        database_url="postgresql://db",
        experiment_id="pilot-id",
        experiment_kind="pilot",
        expected_preparations=6,
        expected_trials=36,
    )


def test_confirmation_gate_validation_fails_closed():
    base = _completed_report("confirmation", "confirmation-id")
    with pytest.raises(RuntimeError, match="did not authorize"):
        acceptance._require_confirmation_gates(
            {**base, "claim_authorized": False, "gates": {"retrieval": True}}
        )
    with pytest.raises(RuntimeError, match="did not authorize"):
        acceptance._require_confirmation_gates(
            {**base, "claim_authorized": True, "gates": {"retrieval": False}}
        )


def test_live_workflow_calls_shared_acceptance_commands():
    workflow = (ROOT / ".github" / "workflows" / "live-acceptance.yml").read_text()

    assert "scripts/run_live_acceptance.py hosted-product --phase providers" in workflow
    for phase in ("semantic", "consolidation", "worker", "browser", "roles"):
        assert f"scripts/run_live_acceptance.py hosted-product --phase {phase}" in workflow
    for forbidden in (
        "learning-pilot",
        "learning-full",
        "run_learning_benchmark.py",
        "finalize-interrupted",
        "configure_changefeed.py pause",
        "live-benchmark-",
    ):
        assert forbidden not in workflow


def test_learning_workflow_is_manual_guarded_and_never_deploys():
    workflow = (ROOT / ".github" / "workflows" / "learning-evidence.yml").read_text()

    assert "workflow_dispatch:" in workflow
    assert 'HINDSIGHT_BENCHMARK_TENANT_ID: 00000000-0000-0000-0000-000000000004' in workflow
    assert "manage_learning_authority.py" in workflow
    assert "run_learning_benchmark.py pilot" in workflow
    assert "run_learning_benchmark.py preregister" in workflow
    assert "run_learning_benchmark.py confirmation" in workflow
    assert "finalize_learning_study.py" in workflow
    assert "configure_changefeed.py pause" not in workflow
    assert "protocol_reset_id" not in workflow
    assert "deploy-demo.yml" not in workflow
    assert "pull_request:" not in workflow
