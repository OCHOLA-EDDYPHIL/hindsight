import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ci_changes", ROOT / "scripts/ci_changes.py")
assert SPEC is not None and SPEC.loader is not None
ci_changes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_changes)
COMPONENTS = ci_changes.COMPONENTS
classify_paths = ci_changes.classify_paths
write_github_output = ci_changes.write_github_output


def selection(*enabled: str) -> dict[str, bool]:
    return {component: component in enabled for component in COMPONENTS}


@pytest.mark.parametrize("event_name", ["pull_request", "push"])
@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/architecture.md",
        "infra/db/README.md",
        "infra/terraform/app/README.md",
        "src/hindsight/README.md",
        "LICENSE",
    ],
)
def test_documentation_wins_before_component_directories(event_name: str, path: str):
    assert classify_paths([path], event_name=event_name) == selection()


@pytest.mark.parametrize("event_name", ["pull_request", "push"])
@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (
            ["src/hindsight/memory.py"],
            selection("python_static", "database", "lambda_artifacts"),
        ),
        (
            ["migrations/0026_future.sql"],
            selection("python_static", "database", "main_qualification"),
        ),
        (
            ["infra/db/roles.sql"],
            selection("python_static", "database", "main_qualification"),
        ),
        (
            ["frontend/src/App.tsx"],
            selection("python_static", "frontend", "lambda_artifacts"),
        ),
        (
            ["src/hindsight/web/assets/app.js"],
            selection("python_static", "frontend", "lambda_artifacts"),
        ),
        (
            ["infra/terraform/app/main.tf"],
            selection("python_static", "terraform"),
        ),
        (
            ["pyproject.toml", "uv.lock"],
            selection(
                "python_static",
                "database",
                "main_qualification",
                "lambda_artifacts",
            ),
        ),
        (
            ["package-lock.json"],
            selection("python_static", "frontend", "lambda_artifacts"),
        ),
        (
            ["docker-compose.yml"],
            selection("python_static", "database", "main_qualification"),
        ),
        (
            ["tests/test_api.py"],
            selection("python_static"),
        ),
        (
            ["tests/test_memory.py"],
            selection("python_static", "database"),
        ),
        (
            ["tests/test_migrations_and_roles.py"],
            selection("python_static", "main_qualification"),
        ),
    ],
)
def test_explicit_component_matrix_is_identical_for_pr_and_main(
    event_name: str, paths: list[str], expected: dict[str, bool]
):
    assert classify_paths(paths, event_name=event_name) == expected


@pytest.mark.parametrize("event_name", ["pull_request", "push"])
def test_multi_component_changes_union_owned_checks(event_name: str):
    assert classify_paths(
        ["README.md", "src/hindsight/api.py", "frontend/src/App.tsx", "infra/terraform/app/main.tf"],
        event_name=event_name,
    ) == selection(
        "python_static",
        "database",
        "frontend",
        "lambda_artifacts",
        "terraform",
    )


@pytest.mark.parametrize("event_name", ["pull_request", "push"])
@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "scripts/ci_changes.py",
        ".github/workflows/future-control.yml",
        "scripts/unclassified_new_tool.py",
        "unclassified.config",
    ],
)
def test_ci_control_and_unknown_paths_fail_closed(event_name: str, path: str):
    assert classify_paths([path], event_name=event_name) == selection(*COMPONENTS)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            ".github/workflows/deploy-demo.yml",
            selection(
                "python_static",
                "database",
                "main_qualification",
                "lambda_artifacts",
                "terraform",
            ),
        ),
        (
            ".github/workflows/destroy-demo.yml",
            selection("python_static", "lambda_artifacts", "terraform"),
        ),
        (
            ".github/workflows/live-acceptance.yml",
            selection(*COMPONENTS),
        ),
        (
            ".github/workflows/migration-compatibility.yml",
            selection("python_static", "database", "main_qualification"),
        ),
        (
            ".github/workflows/plan-bootstrap.yml",
            selection("python_static", "terraform"),
        ),
        (
            ".github/workflows/recovery-drill.yml",
            selection("python_static", "database", "main_qualification"),
        ),
        (
            ".github/workflows/tenant-lifecycle.yml",
            selection(
                "python_static",
                "database",
                "main_qualification",
                "terraform",
            ),
        ),
        (
            ".github/workflows/verify-deployed.yml",
            selection("python_static", "database", "frontend"),
        ),
    ],
)
def test_manual_workflow_controls_select_only_owned_components(
    path: str, expected: dict[str, bool]
):
    assert classify_paths([path], event_name="pull_request") == expected


@pytest.mark.parametrize(
    "path",
    [
        "scripts/provision_lifecycle_database_credential.py",
        "scripts/provision_lifecycle_fixture.py",
    ],
)
def test_lifecycle_provisioners_select_database_checks(path: str):
    assert classify_paths([path], event_name="pull_request") == selection(
        "python_static", "database"
    )


def test_bootstrap_plan_validator_selects_static_and_terraform_checks():
    assert classify_paths(
        ["scripts/validate_bootstrap_plan.py"], event_name="pull_request"
    ) == selection("python_static", "terraform")


@pytest.mark.parametrize("event_name", ["pull_request", "push"])
def test_empty_diff_fails_closed(event_name: str):
    assert classify_paths([], event_name=event_name) == selection(*COMPONENTS)


def test_changed_paths_uses_event_specific_range_and_exposes_both_sides_of_renames(
    monkeypatch,
):
    calls: list[list[str]] = []

    class Result:
        stdout = "old.py\nnew.md\n"

    def fake_run(command, **_kwargs):
        calls.append(command)
        return Result()

    monkeypatch.setattr(ci_changes.subprocess, "run", fake_run)

    assert ci_changes.changed_paths(
        event_name="pull_request", base_sha="base", head_sha="head"
    ) == ["old.py", "new.md"]
    assert ci_changes.changed_paths(
        event_name="push", base_sha="base", head_sha="head"
    ) == ["old.py", "new.md"]
    assert calls == [
        ["git", "diff", "--name-only", "--no-renames", "base...head"],
        ["git", "diff", "--name-only", "--no-renames", "base..head"],
    ]


def test_github_outputs_are_explicit_booleans(tmp_path: Path):
    output = tmp_path / "github-output"
    selected = classify_paths(["frontend/src/App.tsx"], event_name="pull_request")

    write_github_output(output, selected)

    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert set(values) == set(COMPONENTS)
    assert set(values.values()) <= {"true", "false"}


def test_unsupported_events_are_rejected():
    with pytest.raises(ValueError, match="unsupported normal CI event"):
        classify_paths(["README.md"], event_name="schedule")
