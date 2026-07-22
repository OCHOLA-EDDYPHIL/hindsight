"""Classify pull-request paths into fail-closed CI component selections."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from pathlib import Path
import subprocess


COMPONENTS = (
    "database",
    "migrations",
    "diagnostics",
    "frontend",
    "lambda_artifacts",
    "terraform",
)
DATABASE_TEST_FILES = frozenset(
    {
        "test_agent.py",
        "test_benchmark_protocol_migrations.py",
        "test_consolidation.py",
        "test_cross_episode_demo.py",
        "test_dashboard.py",
        "test_embedding_rotation.py",
        "test_governed_memory.py",
        "test_learning_benchmark_setup.py",
        "test_learning_evidence_foundation.py",
        "test_learning_orchestration.py",
        "test_mcp_server.py",
        "test_memory.py",
        "test_migrations_and_roles.py",
        "test_operation_retries.py",
        "test_poison_rewind_demo.py",
        "test_run_attempts.py",
        "test_run_dispatch.py",
        "test_runs.py",
        "test_smoke.py",
        "test_system_of_record.py",
        "test_telemetry.py",
        "test_tenant_isolation.py",
        "test_trace_contract.py",
    }
)
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


def _all_selected() -> dict[str, bool]:
    return {component: True for component in COMPONENTS}


def classify_paths(paths: Iterable[str], *, event_name: str) -> dict[str, bool]:
    """Return the component jobs required for one GitHub event."""

    if event_name != "pull_request":
        return _all_selected()

    selected = {component: False for component in COMPONENTS}
    saw_path = False
    for raw_path in paths:
        path = raw_path.strip()
        if path.startswith("./"):
            path = path[2:]
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
            selected["database"] = True
            selected["lambda_artifacts"] = True
            if _affects_diagnostics(path):
                selected["diagnostics"] = True
            continue
        if path.startswith("migrations/") or path.startswith("infra/db/"):
            selected["database"] = True
            selected["migrations"] = True
            selected["lambda_artifacts"] = True
            continue
        if path.startswith("fixtures/"):
            selected["database"] = True
            selected["diagnostics"] = True
            selected["lambda_artifacts"] = True
            continue
        if path.startswith("docs/") or path.endswith((".md", ".rst", ".txt")):
            continue
        if path.startswith("tests/"):
            name = Path(path).name
            if name in DATABASE_TEST_FILES:
                selected["database"] = True
            if name in MIGRATION_TEST_FILES:
                selected["migrations"] = True
            continue
        if path.startswith("scripts/"):
            name = Path(path).name
            if name in {"migrate.py", "apply_database_roles.py", "initialize_agent_storage.py"}:
                selected["database"] = True
                selected["migrations"] = True
            elif name in {"run_rank_diagnostics.py", "run_learning_benchmark.py"}:
                selected["diagnostics"] = True
            elif name in {"build_lambda_artifacts.py", "smoke_lambda_artifacts.py"}:
                selected["lambda_artifacts"] = True
            continue
        return _all_selected()

    if not saw_path:
        return _all_selected()
    return selected


def _forces_full_matrix(path: str) -> bool:
    return (
        path.startswith(".github/workflows/")
        or path.startswith(".github/actions/")
        or path.startswith("scripts/ci_")
        or path in {
            "tests/test_ci_aggregate.py",
            "tests/test_ci_changes.py",
            "tests/test_ci_contracts.py",
        }
        or path
        in {
            "Makefile",
            "docker-compose.yml",
            "pyproject.toml",
            "uv.lock",
        }
    )


def _affects_diagnostics(path: str) -> bool:
    return Path(path).name in {
        "benchmark.py",
        "embedding_index.py",
        "embeddings.py",
        "memory.py",
    }


def changed_paths(*, base_sha: str, head_sha: str) -> list[str]:
    if not base_sha or not head_sha:
        raise ValueError("base and head SHA are required for pull-request classification")
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

    paths = (
        changed_paths(base_sha=args.base_sha, head_sha=args.head_sha)
        if args.event_name == "pull_request"
        else []
    )
    selected = classify_paths(paths, event_name=args.event_name)
    write_github_output(args.github_output, selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
