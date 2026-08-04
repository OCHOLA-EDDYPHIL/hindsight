"""Classify changed paths into fail-closed CI qualification tiers."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ci_test_groups import database_test_files  # noqa: E402


COMPONENTS = (
    "database",
    "main_qualification",
    "migrations",
    "research",
    "frontend",
    "lambda_artifacts",
    "terraform",
)
DATABASE_TEST_FILES = frozenset(Path(path).name for path in database_test_files("core"))
RESEARCH_TEST_FILES = frozenset(Path(path).name for path in database_test_files("research"))
MIGRATION_TEST_FILES = frozenset(
    {
        "test_agent.py",
        "test_benchmark_protocol_migrations.py",
        "test_learning_benchmark_setup.py",
        "test_learning_evidence_foundation.py",
        "test_migrations_and_roles.py",
        "test_run_dispatch.py",
    }
)
RESEARCH_MODULES = frozenset(
    {
        "benchmark.py",
        "evidence_archive.py",
        "learning_authority.py",
        "qualification_authority.py",
        "rank_diagnostics.py",
        "representation_selection.py",
        "v4_corpus.py",
    }
)
SHARED_RESEARCH_MODULES = frozenset(
    {
        "embedding_index.py",
        "embeddings.py",
        "memory.py",
    }
)


def _all_selected() -> dict[str, bool]:
    return {component: True for component in COMPONENTS}


def classify_paths(paths: Iterable[str], *, event_name: str) -> dict[str, bool]:
    """Return the component jobs required for one GitHub event."""

    if event_name not in {"pull_request", "push"}:
        return _all_selected()

    selected = {component: False for component in COMPONENTS}
    saw_path = False
    for raw_path in paths:
        path = raw_path.strip().removeprefix("./")
        if not path:
            continue
        saw_path = True
        if _forces_full_matrix(path):
            return _all_selected()
        if path.startswith("frontend/") or path in {"package.json", "package-lock.json"}:
            selected["frontend"] = True
            selected["lambda_artifacts"] = True
            continue
        if path.startswith("infra/terraform/"):
            selected["terraform"] = True
            selected["lambda_artifacts"] = True
            continue
        if path.startswith("src/hindsight/web/"):
            selected["frontend"] = True
            selected["lambda_artifacts"] = True
            continue
        if path.startswith("src/hindsight/"):
            name = Path(path).name
            if name in RESEARCH_MODULES:
                selected["research"] = True
            else:
                selected["database"] = True
            if name in SHARED_RESEARCH_MODULES:
                selected["research"] = True
            if name == "db.py":
                selected["migrations"] = True
            selected["lambda_artifacts"] = True
            continue
        if path.startswith("migrations/") or path.startswith("infra/db/"):
            selected["database"] = True
            selected["migrations"] = True
            selected["lambda_artifacts"] = True
            continue
        if path.startswith("fixtures/"):
            selected["research"] = True
            selected["lambda_artifacts"] = True
            continue
        if path.startswith("docs/") or path.endswith((".md", ".rst", ".txt")):
            continue
        if path.startswith("tests/"):
            name = Path(path).name
            if name in DATABASE_TEST_FILES:
                selected["database"] = True
            if name in RESEARCH_TEST_FILES:
                selected["research"] = True
            if name in MIGRATION_TEST_FILES:
                selected["migrations"] = True
            continue
        if path.startswith("scripts/"):
            name = Path(path).name
            if name in {
                "apply_database_roles.py",
                "init_db.py",
                "initialize_agent_storage.py",
                "migrate.py",
                "populated_upgrade_fixture.py",
                "run_migration_compatibility.py",
                "schema_manifest.py",
            }:
                selected["database"] = True
                selected["migrations"] = True
            elif name in {
                "manage_v4_corpus.py",
                "review_v4_corpus.py",
                "run_learning_benchmark.py",
                "run_rank_diagnostics.py",
            }:
                selected["research"] = True
            elif name in {"build_lambda_artifacts.py", "smoke_lambda_artifacts.py"}:
                selected["lambda_artifacts"] = True
            continue
        return _all_selected()

    if not saw_path:
        return _all_selected()
    if event_name == "push":
        selected["database"] = True
        selected["main_qualification"] = True
        selected["lambda_artifacts"] = True
    return selected


def _forces_full_matrix(path: str) -> bool:
    return (
        path.startswith(".github/workflows/")
        or path.startswith(".github/actions/")
        or path.startswith("scripts/ci_")
        or path
        in {
            "tests/test_ci_aggregate.py",
            "tests/test_ci_changes.py",
            "tests/test_ci_contracts.py",
            "Makefile",
            "docker-compose.yml",
            "pyproject.toml",
            "uv.lock",
        }
    )


def changed_paths(*, base_sha: str, head_sha: str) -> list[str]:
    if not base_sha or not head_sha:
        raise ValueError("base and head SHA are required for path classification")
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_sha}...{head_sha}"],
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
    paths = (
        changed_paths(base_sha=args.base_sha, head_sha=args.head_sha) if diffable else []
    )
    selected = classify_paths(paths, event_name=args.event_name)
    write_github_output(args.github_output, selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
