import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ci_changes", ROOT / "scripts/ci_changes.py")
assert SPEC is not None and SPEC.loader is not None
ci_changes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_changes)
COMPONENTS = ci_changes.COMPONENTS
classify_paths = ci_changes.classify_paths
write_github_output = ci_changes.write_github_output
GROUP_SPEC = importlib.util.spec_from_file_location(
    "ci_test_groups", ROOT / "scripts/ci_test_groups.py"
)
assert GROUP_SPEC is not None and GROUP_SPEC.loader is not None
ci_test_groups = importlib.util.module_from_spec(GROUP_SPEC)
GROUP_SPEC.loader.exec_module(ci_test_groups)


def test_main_always_runs_core_database_artifacts_and_schema_qualification():
    assert classify_paths(["docs/architecture.md"], event_name="push") == {
        "database": True,
        "main_qualification": True,
        "migrations": False,
        "research": False,
        "frontend": False,
        "lambda_artifacts": True,
        "terraform": False,
    }


def test_path_classifier_tracks_database_and_migration_test_inventory():
    assert ci_changes.DATABASE_TEST_FILES == {
        Path(path).name
        for group in ("core_a", "core_b")
        for path in ci_test_groups.database_test_files(group)
    }
    assert ci_changes.RESEARCH_TEST_FILES == {
        Path(path).name for path in ci_test_groups.database_test_files("research")
    }
    assert ci_changes.MIGRATION_TEST_FILES == {
        Path(node.split("::", 1)[0]).name
        for node in ci_test_groups.MIGRATION_CASES.values()
    }


def test_frontend_and_terraform_paths_select_only_their_dependencies():
    selected = classify_paths(
        ["frontend/src/App.tsx", "infra/terraform/app/main.tf"],
        event_name="pull_request",
    )

    assert selected == {
        "database": False,
        "main_qualification": False,
        "migrations": False,
        "research": False,
        "frontend": True,
        "lambda_artifacts": True,
        "terraform": True,
    }


def test_built_web_assets_require_frontend_freshness_and_lambda_packaging():
    selected = classify_paths(
        ["./src/hindsight/web/assets/app.js"], event_name="pull_request"
    )

    assert selected == {
        "database": False,
        "main_qualification": False,
        "migrations": False,
        "research": False,
        "frontend": True,
        "lambda_artifacts": True,
        "terraform": False,
    }


def test_backend_paths_select_database_and_lambda_with_targeted_research():
    ordinary = classify_paths(
        ["src/hindsight/trace_contract.py"], event_name="pull_request"
    )
    diagnostic = classify_paths(
        ["src/hindsight/embeddings.py"], event_name="pull_request"
    )

    assert ordinary["database"] is True
    assert ordinary["lambda_artifacts"] is True
    assert ordinary["research"] is False
    assert diagnostic["research"] is True


def test_ci_control_and_unknown_paths_fail_safe_to_full_matrix():
    for path in (
        ".github/workflows/ci.yml",
        "tests/test_ci_changes.py",
        "unclassified.config",
    ):
        selected = classify_paths([path], event_name="pull_request")
        assert all(selected.values()), path


def test_ordinary_unit_and_deployment_tool_changes_use_static_ci_only():
    selected = classify_paths(
        ["tests/test_api.py", "tests/test_deployment_tools.py", "scripts/configure_changefeed.py"],
        event_name="pull_request",
    )

    assert not any(selected.values())


def test_database_tests_do_not_select_migrations_unless_migration_sensitive():
    ordinary = classify_paths(["tests/test_memory.py"], event_name="pull_request")
    migration = classify_paths(
        ["tests/test_migrations_and_roles.py"], event_name="pull_request"
    )

    assert ordinary["database"] is True
    assert ordinary["migrations"] is False
    assert migration["database"] is True
    assert migration["migrations"] is True
    assert migration["research"] is False


def test_migration_paths_select_database_replay_and_packaging_only():
    selected = classify_paths(
        ["migrations/0026_future.sql"], event_name="pull_request"
    )

    assert selected == {
        "database": True,
        "main_qualification": False,
        "migrations": True,
        "research": False,
        "frontend": False,
        "lambda_artifacts": True,
        "terraform": False,
    }


def test_research_checks_run_only_for_their_own_inputs():
    ordinary = classify_paths(
        ["scripts/configure_changefeed.py"], event_name="pull_request"
    )
    research = classify_paths(
        ["scripts/run_rank_diagnostics.py"], event_name="pull_request"
    )

    assert ordinary["research"] is False
    assert research["research"] is True


def test_documentation_only_pull_request_keeps_component_jobs_disabled():
    selected = classify_paths(["docs/architecture.md"], event_name="pull_request")

    assert not any(selected.values())


def test_empty_pull_request_diff_fails_safe_to_full_matrix():
    selected = classify_paths([], event_name="pull_request")

    assert all(selected.values())


def test_github_outputs_are_explicit_booleans(tmp_path: Path):
    output = tmp_path / "github-output"
    selected = classify_paths(["frontend/src/App.tsx"], event_name="pull_request")

    write_github_output(output, selected)

    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert set(values) == set(COMPONENTS)
    assert set(values.values()) <= {"true", "false"}
