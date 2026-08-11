"""Select the normal CI jobs affected by changed repository paths."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ci_test_groups import database_test_files  # noqa: E402


COMPONENTS = (
    "python_static",
    "database",
    "main_qualification",
    "frontend",
    "lambda_artifacts",
    "terraform",
)
ALL_COMPONENTS = frozenset(COMPONENTS)
PRODUCT_TEST_FILES = frozenset(database_test_files("product"))
MAIN_QUALIFICATION_TEST_FILES = frozenset(database_test_files("main_extended"))

CI_ALL_COMPONENT_FILES = frozenset(
    {
        ".github/workflows/ci.yml",
        "scripts/ci_changes.py",
    }
)
CI_STATIC_FILES = frozenset(
    {
        "scripts/run_affected_ci.py",
        "scripts/verify_ci_components.py",
        "scripts/verify_ci_provenance.py",
        "tests/test_ci_aggregate.py",
        "tests/test_ci_changes.py",
        "tests/test_ci_contracts.py",
        "tests/test_ci_migration_runner.py",
        "tests/test_ci_provenance.py",
    }
)
WORKFLOW_COMPONENTS = {
    ".github/workflows/capacity-qualification.yml": frozenset(
        {"database", "main_qualification"}
    ),
    ".github/workflows/deploy-demo.yml": frozenset(
        {"database", "main_qualification", "lambda_artifacts", "terraform"}
    ),
    ".github/workflows/destroy-demo.yml": frozenset(
        {"lambda_artifacts", "terraform"}
    ),
    ".github/workflows/evidence-reuse.yml": frozenset(
        {"database", "main_qualification"}
    ),
    ".github/workflows/live-acceptance.yml": frozenset(
        {"database", "main_qualification", "frontend", "lambda_artifacts", "terraform"}
    ),
    ".github/workflows/migration-compatibility.yml": frozenset(
        {"database", "main_qualification"}
    ),
    ".github/workflows/plan-bootstrap.yml": frozenset({"terraform"}),
    ".github/workflows/provision-lifecycle-fixture.yml": frozenset(
        {"database", "main_qualification", "terraform"}
    ),
    ".github/workflows/recovery-drill.yml": frozenset(
        {"database", "main_qualification"}
    ),
    ".github/workflows/tenant-lifecycle.yml": frozenset(
        {"database", "main_qualification", "terraform"}
    ),
    ".github/workflows/verify-deployed.yml": frozenset({"database", "frontend"}),
}
MAIN_QUALIFICATION_SCRIPTS = frozenset(
    {
        "apply_database_roles.py",
        "run_capacity_qualification.py",
        "drop_diagnostic_database.py",
        "evidence_reuse.py",
        "initialize_agent_storage.py",
        "migrate.py",
        "populated_upgrade_fixture.py",
        "schema_manifest.py",
        "validate_capacity_evidence.py",
    }
)
DATABASE_SCRIPTS = frozenset(
    {
        *MAIN_QUALIFICATION_SCRIPTS,
        "configure_changefeed.py",
        "provision_lifecycle_database_credential.py",
        "provision_lifecycle_fixture.py",
        "reembed_memories.py",
        "run_incident_agent.py",
    }
)
LAMBDA_SCRIPTS = frozenset(
    {"build_lambda_artifacts.py", "smoke_lambda_artifacts.py"}
)
TERRAFORM_SCRIPTS = frozenset({"validate_bootstrap_plan.py"})


def _none_selected() -> dict[str, bool]:
    return {component: False for component in COMPONENTS}


def _select(selected: dict[str, bool], *components: str) -> None:
    for component in components:
        selected[component] = True


def _is_documentation(path: str) -> bool:
    lowered = path.lower()
    return (
        lowered == "license"
        or lowered.startswith("docs/")
        or lowered.endswith(".md")
    )


def classify_paths(paths: Iterable[str], *, event_name: str) -> dict[str, bool]:
    """Return the same conservative component selection for PR and main events."""

    if event_name not in {"pull_request", "push"}:
        raise ValueError(f"unsupported normal CI event: {event_name}")

    selected = _none_selected()
    saw_path = False
    for raw_path in paths:
        path = raw_path.strip().removeprefix("./")
        if not path:
            continue
        saw_path = True

        # Documentation must win before component-directory ownership.
        if _is_documentation(path):
            continue

        _select(selected, "python_static")

        if path in CI_ALL_COMPONENT_FILES:
            _select(selected, *ALL_COMPONENTS)
            continue
        if path in WORKFLOW_COMPONENTS:
            _select(selected, *WORKFLOW_COMPONENTS[path])
            continue
        if path.startswith(".github/workflows/"):
            _select(selected, *ALL_COMPONENTS)
            continue
        if path in CI_STATIC_FILES:
            continue
        if path == "scripts/ci_test_groups.py":
            _select(selected, "database", "main_qualification")
            continue

        if path.startswith("frontend/") or path in {
            "components.json",
            "package.json",
            "package-lock.json",
        }:
            _select(selected, "frontend", "lambda_artifacts")
            continue
        if path.startswith("src/hindsight/web/"):
            _select(selected, "frontend", "lambda_artifacts")
            continue
        if path.startswith("infra/terraform/"):
            _select(selected, "terraform")
            continue
        if path.startswith("src/hindsight/"):
            _select(selected, "database", "lambda_artifacts")
            continue
        if path.startswith("migrations/") or path.startswith("infra/db/"):
            _select(selected, "database", "main_qualification")
            continue
        if path.startswith("queries/") or path.startswith("fixtures/"):
            _select(selected, "database")
            continue

        if path.startswith("tests/"):
            if path in PRODUCT_TEST_FILES:
                _select(selected, "database")
            if path in MAIN_QUALIFICATION_TEST_FILES:
                _select(selected, "main_qualification")
            continue

        if path.startswith("scripts/"):
            name = Path(path).name
            if name in DATABASE_SCRIPTS:
                _select(selected, "database")
            if name in MAIN_QUALIFICATION_SCRIPTS:
                _select(selected, "main_qualification")
            if name in LAMBDA_SCRIPTS:
                _select(selected, "lambda_artifacts")
            if name in TERRAFORM_SCRIPTS:
                _select(selected, "terraform")
            if (
                name in DATABASE_SCRIPTS
                or name in LAMBDA_SCRIPTS
                or name in TERRAFORM_SCRIPTS
            ):
                continue
            _select(selected, *ALL_COMPONENTS)
            continue

        if path in {"pyproject.toml", "uv.lock", ".python-version"}:
            _select(selected, "database", "main_qualification", "lambda_artifacts")
            continue
        if path == "docker-compose.yml":
            _select(selected, "database", "main_qualification")
            continue
        if path in {".env.example", "Makefile", ".gitignore"}:
            continue

        _select(selected, *ALL_COMPONENTS)

    if not saw_path:
        _select(selected, *ALL_COMPONENTS)
    return selected


def changed_paths(*, event_name: str, base_sha: str, head_sha: str) -> list[str]:
    if event_name not in {"pull_request", "push"}:
        raise ValueError(f"unsupported normal CI event: {event_name}")
    if not base_sha or not head_sha:
        raise ValueError("base and head SHA are required for path classification")
    separator = "..." if event_name == "pull_request" else ".."
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--no-renames",
            f"{base_sha}{separator}{head_sha}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def write_github_output(path: Path, selected: dict[str, bool]) -> None:
    with path.open("a", encoding="utf-8") as output:
        for component in COMPONENTS:
            output.write(f"{component}={'true' if selected[component] else 'false'}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--head-sha", default="")
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()

    diffable = (
        args.event_name in {"pull_request", "push"}
        and args.base_sha
        and args.head_sha
        and set(args.base_sha) != {"0"}
    )
    paths: list[str] = []
    if diffable:
        try:
            paths = changed_paths(
                event_name=args.event_name,
                base_sha=args.base_sha,
                head_sha=args.head_sha,
            )
        except subprocess.CalledProcessError as exc:
            print(f"unable to classify changed paths: {exc}", file=sys.stderr)
    selected = classify_paths(paths, event_name=args.event_name)
    write_github_output(args.github_output, selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
