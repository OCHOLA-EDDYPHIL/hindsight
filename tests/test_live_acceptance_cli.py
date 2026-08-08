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


def _github_outputs(path: pathlib.Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


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


def test_product_provider_verification_uses_only_product_sanity_checks(monkeypatch):
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
    assert calls[0][1]["RUN_LIVE_GEMINI_EMBEDDINGS"] == "1"
    assert calls[0][1]["GEMINI_API_KEY"] == "opaque-material"
    assert calls[0][2:] == ("providers", artifact_dir)

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


def test_full_hosted_plan_preserves_authoritative_path(monkeypatch, tmp_path):
    output = tmp_path / "github-output"
    monkeypatch.setattr(
        acceptance,
        "_probe_deployed_candidate",
        lambda **_kwargs: pytest.fail("full mode must not probe for deployment reuse"),
    )

    acceptance._plan_hosted_acceptance(
        SimpleNamespace(
            mode="full",
            requested_sha="a" * 40,
            candidate_ui_url="https://ui.example.test",
            github_output=output,
        )
    )

    assert _github_outputs(output) == {
        "acceptance_mode": "full",
        "run_product_preflight": "true",
        "run_deploy": "true",
        "reuse_candidate": "false",
        "candidate_ui_url": "",
        "candidate_api_url": "",
        "candidate_websocket_url": "",
        "observed_revision": "not-checked",
    }


def test_browser_only_plan_reuses_only_an_exact_candidate(monkeypatch, tmp_path):
    output = tmp_path / "github-output"
    requested_sha = "b" * 40
    monkeypatch.setattr(
        acceptance,
        "_probe_deployed_candidate",
        lambda **_kwargs: {
            "reusable": True,
            "observed_revision": requested_sha,
            "ui_url": "https://ui.example.test",
            "api_url": "https://ui.example.test",
            "websocket_url": "wss://socket.example.test/demo",
        },
    )

    acceptance._plan_hosted_acceptance(
        SimpleNamespace(
            mode="browser-only",
            requested_sha=requested_sha,
            candidate_ui_url="https://ui.example.test",
            github_output=output,
        )
    )

    assert _github_outputs(output) == {
        "acceptance_mode": "browser-only",
        "run_product_preflight": "false",
        "run_deploy": "false",
        "reuse_candidate": "true",
        "candidate_ui_url": "https://ui.example.test",
        "candidate_api_url": "https://ui.example.test",
        "candidate_websocket_url": "wss://socket.example.test/demo",
        "observed_revision": requested_sha,
    }


@pytest.mark.parametrize("observed_revision", ("a" * 40, "unavailable"))
def test_browser_only_plan_deploys_once_for_nonreusable_candidate(
    monkeypatch, tmp_path, observed_revision
):
    output = tmp_path / "github-output"
    monkeypatch.setattr(
        acceptance,
        "_probe_deployed_candidate",
        lambda **_kwargs: {
            "reusable": False,
            "observed_revision": observed_revision,
        },
    )

    acceptance._plan_hosted_acceptance(
        SimpleNamespace(
            mode="browser-only",
            requested_sha="b" * 40,
            candidate_ui_url="https://ui.example.test",
            github_output=output,
        )
    )

    outputs = _github_outputs(output)
    assert outputs["run_product_preflight"] == "false"
    assert outputs["run_deploy"] == "true"
    assert outputs["reuse_candidate"] == "false"
    assert outputs["observed_revision"] == observed_revision


def test_candidate_probe_requires_exact_revision_and_runtime_endpoints(monkeypatch):
    requested_sha = "c" * 40
    monkeypatch.setattr(
        acceptance,
        "_read_runtime_config",
        lambda _url: {
            "apiBase": "/v1",
            "websocketUrl": "wss://socket.example.test/demo",
        },
    )
    monkeypatch.setattr(
        acceptance,
        "_read_json_url",
        lambda _url: {"status": "live", "revision": requested_sha},
    )

    exact = acceptance._probe_deployed_candidate(
        expected_sha=requested_sha,
        ui_url="https://ui.example.test",
    )
    assert exact == {
        "reusable": True,
        "observed_revision": requested_sha,
        "ui_url": "https://ui.example.test",
        "api_url": "https://ui.example.test",
        "websocket_url": "wss://socket.example.test/demo",
    }

    monkeypatch.setattr(
        acceptance,
        "_read_json_url",
        lambda _url: {"status": "live", "revision": "d" * 40},
    )
    assert acceptance._probe_deployed_candidate(
        expected_sha=requested_sha,
        ui_url="https://ui.example.test",
    ) == {"reusable": False, "observed_revision": "d" * 40}


def test_browser_preflight_fails_closed_on_revision_mismatch(monkeypatch):
    monkeypatch.setenv("HINDSIGHT_BROWSER_BASE_URL", "https://ui.example.test")
    monkeypatch.setenv("HOSTED_API_URL", "https://api.example.test")
    monkeypatch.setenv("HINDSIGHT_WEBSOCKET_URL", "wss://socket.example.test/demo")
    monkeypatch.setenv("HINDSIGHT_EXPECTED_DEPLOYED_REVISION", "e" * 40)
    monkeypatch.setattr(
        acceptance,
        "_read_json_url",
        lambda _url: {"status": "live", "revision": "f" * 40},
    )

    with pytest.raises(RuntimeError, match="does not match"):
        acceptance._verify_hosted_endpoints()


def test_browser_revision_mismatch_stops_before_changefeed_or_pytest(monkeypatch):
    monkeypatch.setattr(acceptance, "_require_gemini_credentials", lambda: None)
    monkeypatch.setattr(
        acceptance,
        "_verify_hosted_endpoints",
        lambda: (_ for _ in ()).throw(RuntimeError("revision mismatch")),
    )
    monkeypatch.setattr(
        acceptance,
        "_verify_changefeed",
        lambda _env: pytest.fail("changefeed check must not run after a SHA mismatch"),
    )
    monkeypatch.setattr(
        acceptance,
        "_run_hosted_pytest",
        lambda *_args, **_kwargs: pytest.fail("browser mutations must not start"),
    )

    with pytest.raises(RuntimeError, match="revision mismatch"):
        acceptance._run_hosted_product(
            SimpleNamespace(
                phase="browser",
                database_url=(
                    "postgresql://runtime@cluster.example:26257/"
                    "hindsight?sslmode=verify-full"
                ),
            )
        )


@pytest.mark.parametrize("revision", ("short", "A" * 40, "g" * 40))
def test_hosted_plan_rejects_noncanonical_requested_sha(tmp_path, revision):
    with pytest.raises(ValueError, match="full lowercase hexadecimal SHA"):
        acceptance._plan_hosted_acceptance(
            SimpleNamespace(
                mode="full",
                requested_sha=revision,
                candidate_ui_url="https://ui.example.test",
                github_output=tmp_path / "github-output",
            )
        )


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
