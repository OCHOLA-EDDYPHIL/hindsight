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


def test_main_adds_product_schema_lambda_and_main_qualification():
    assert classify_paths(["docs/architecture.md"], event_name="push") == {
        "database": True,
        "main_qualification": True,
        "frontend": False,
        "lambda_artifacts": True,
        "terraform": False,
    }


def test_path_classifier_tracks_only_fast_product_test_inventory():
    assert ci_changes.PRODUCT_TEST_FILES == {
        Path(path).name for path in ci_test_groups.database_test_files("product")
    }


def test_frontend_and_terraform_paths_select_only_their_dependencies():
    selected = classify_paths(
        ["frontend/src/App.tsx", "infra/terraform/app/main.tf"],
        event_name="pull_request",
    )

    assert selected == {
        "database": False,
        "main_qualification": False,
        "frontend": True,
        "lambda_artifacts": True,
        "terraform": True,
    }


def test_backend_and_fresh_migration_paths_select_product_and_packaging_only():
    for path in ("src/hindsight/memory.py", "migrations/0026_future.sql"):
        selected = classify_paths([path], event_name="pull_request")
        assert selected == {
            "database": True,
            "main_qualification": False,
            "frontend": False,
            "lambda_artifacts": True,
            "terraform": False,
        }


def test_all_owned_database_tests_select_the_database_job():
    selected = classify_paths(
        ["tests/test_learning_evidence_foundation.py"], event_name="pull_request"
    )

    assert selected["database"] is True


def test_ci_control_changes_do_not_force_expensive_jobs():
    for path in (
        ".github/workflows/ci.yml",
        ".github/workflows/migration-compatibility.yml",
        "scripts/ci_changes.py",
        "scripts/run_affected_ci.py",
        "tests/test_ci_contracts.py",
    ):
        selected = classify_paths([path], event_name="pull_request")
        assert not any(selected.values()), path


def test_test_group_ownership_change_selects_one_database_job_only():
    selected = classify_paths(["scripts/ci_test_groups.py"], event_name="pull_request")

    assert selected == {
        "database": True,
        "main_qualification": False,
        "frontend": False,
        "lambda_artifacts": False,
        "terraform": False,
    }


def test_unknown_and_empty_diffs_fail_safe_to_product_database_only():
    expected = {
        "database": True,
        "main_qualification": False,
        "frontend": False,
        "lambda_artifacts": False,
        "terraform": False,
    }
    assert classify_paths(["unclassified.config"], event_name="pull_request") == expected
    assert classify_paths([], event_name="pull_request") == expected


def test_documentation_only_pull_request_keeps_component_jobs_disabled():
    selected = classify_paths(["docs/architecture.md"], event_name="pull_request")

    assert not any(selected.values())


def test_github_outputs_are_explicit_booleans(tmp_path: Path):
    output = tmp_path / "github-output"
    selected = classify_paths(["frontend/src/App.tsx"], event_name="pull_request")

    write_github_output(output, selected)

    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert set(values) == set(COMPONENTS)
    assert set(values.values()) <= {"true", "false"}
