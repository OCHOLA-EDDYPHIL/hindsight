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


def test_main_always_runs_every_component():
    assert classify_paths(["docs/architecture.md"], event_name="push") == {
        component: True for component in COMPONENTS
    }


def test_frontend_and_terraform_paths_select_only_their_dependencies():
    selected = classify_paths(
        ["frontend/src/App.tsx", "infra/terraform/app/main.tf"],
        event_name="pull_request",
    )

    assert selected == {
        "database": False,
        "migrations": False,
        "diagnostics": False,
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
        "migrations": False,
        "diagnostics": False,
        "frontend": True,
        "lambda_artifacts": True,
        "terraform": False,
    }


def test_backend_paths_select_database_and_lambda_with_targeted_diagnostics():
    ordinary = classify_paths(
        ["src/hindsight/trace_contract.py"], event_name="pull_request"
    )
    diagnostic = classify_paths(
        ["src/hindsight/embeddings.py"], event_name="pull_request"
    )

    assert ordinary["database"] is True
    assert ordinary["lambda_artifacts"] is True
    assert ordinary["diagnostics"] is False
    assert diagnostic["diagnostics"] is True


def test_workflow_test_migration_and_unknown_paths_fail_safe_to_full_matrix():
    for path in (
        ".github/workflows/ci.yml",
        "tests/test_api.py",
        "migrations/0023_future.sql",
        "unclassified.config",
    ):
        selected = classify_paths([path], event_name="pull_request")
        assert all(selected.values()), path


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
