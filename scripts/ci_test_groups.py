"""Own and execute the disjoint Python test groups used by local and hosted CI."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

MIGRATION_CASES = {
    "benchmark_upgrade": (
        "tests/test_benchmark_protocol_migrations.py::"
        "test_upgrade_from_0012_backfills_the_first_confirmation_after_schema_commit"
    ),
    "benchmark_fresh": (
        "tests/test_benchmark_protocol_migrations.py::"
        "test_fresh_protocol_enforces_binding_study_and_preparation_invariants"
    ),
    "benchmark_preparation": (
        "tests/test_benchmark_protocol_migrations.py::"
        "test_preparation_attempts_commit_and_exhaust_without_attempt_four"
    ),
    "benchmark_finalizer": (
        "tests/test_benchmark_protocol_migrations.py::"
        "test_interrupted_benchmark_finalizer_closes_children_before_parents"
    ),
    "benchmark_retry": (
        "tests/test_learning_benchmark_setup.py::"
        "test_preparation_retry_reuses_seeded_context"
    ),
    "agent_runtime_roles": (
        "tests/test_agent.py::"
        "test_preinitialized_agent_storage_supports_start_and_resume_without_create_privilege"
    ),
    "populated_roles": (
        "tests/test_migrations_and_roles.py::"
        "test_populated_upgrade_repairs_run_decisions_and_agent_role_can_write"
    ),
    "dispatch_upgrade": (
        "tests/test_run_dispatch.py::"
        "test_outbox_migration_backfills_queued_and_resuming_runs"
    ),
}

DATABASE_GROUPS = {
    "database_a": (
        "tests/test_agent.py",
        "tests/test_benchmark_protocol_migrations.py",
        "tests/test_governed_memory.py",
        "tests/test_memory.py",
        "tests/test_run_attempts.py",
        "tests/test_runs.py",
        "tests/test_tenant_isolation.py",
        "tests/test_trace_contract.py",
    ),
    "database_b": (
        "tests/test_consolidation.py",
        "tests/test_cross_episode_demo.py",
        "tests/test_dashboard.py",
        "tests/test_embedding_rotation.py",
        "tests/test_learning_benchmark_setup.py",
        "tests/test_learning_evidence_foundation.py",
        "tests/test_mcp_server.py",
        "tests/test_migrations_and_roles.py",
        "tests/test_operation_retries.py",
        "tests/test_poison_rewind_demo.py",
        "tests/test_run_dispatch.py",
        "tests/test_smoke.py",
        "tests/test_system_of_record.py",
        "tests/test_telemetry.py",
    ),
}


def all_test_files() -> set[str]:
    return {
        str(path.relative_to(ROOT))
        for path in TESTS.glob("test_*.py")
        if path.is_file()
    }


def database_test_files() -> set[str]:
    return {path for paths in DATABASE_GROUPS.values() for path in paths}


def unit_test_files() -> tuple[str, ...]:
    return tuple(sorted(all_test_files() - database_test_files()))


def _decorated_tests(marker: str) -> set[str]:
    nodes: set[str] = set()
    for relative_path in all_test_files():
        path = ROOT / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for item in tree.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = {ast.unparse(decorator) for decorator in item.decorator_list}
            if marker in decorators:
                nodes.add(f"{relative_path}::{item.name}")
    return nodes


def inventory_errors() -> list[str]:
    errors: list[str] = []
    all_files = all_test_files()
    assigned: dict[str, str] = {}
    for group, paths in DATABASE_GROUPS.items():
        for path in paths:
            if path in assigned:
                errors.append(f"{path} belongs to both {assigned[path]} and {group}")
            assigned[path] = group
            if path not in all_files:
                errors.append(f"{group} references missing test file {path}")
    for case, node in MIGRATION_CASES.items():
        path = node.split("::", 1)[0]
        if path not in assigned:
            errors.append(f"migration case {case} is not owned by a database group")
    database_marked_files = {
        node.split("::", 1)[0] for node in _decorated_tests("requires_db")
    }
    for path in sorted(database_marked_files - set(assigned)):
        errors.append(f"database-marked test file is not assigned to a database group: {path}")
    migration_nodes = _decorated_tests("pytest.mark.migration_acceptance")
    configured_nodes = set(MIGRATION_CASES.values())
    if migration_nodes != configured_nodes:
        errors.append(
            "migration case inventory mismatch: "
            f"missing={sorted(migration_nodes - configured_nodes)} "
            f"extra={sorted(configured_nodes - migration_nodes)}"
        )
    covered = set(assigned).union(unit_test_files())
    if covered != all_files:
        errors.append(
            "test inventory mismatch: "
            f"missing={sorted(all_files - covered)} extra={sorted(covered - all_files)}"
        )
    return errors


def pytest_args(group: str, *, case: str | None = None) -> list[str]:
    if group == "unit":
        return [*unit_test_files()]
    if group in DATABASE_GROUPS:
        return [*DATABASE_GROUPS[group], "-m", "not migration_acceptance"]
    if group == "migration":
        if case not in MIGRATION_CASES:
            raise ValueError(f"unknown migration case: {case}")
        return [MIGRATION_CASES[case]]
    raise ValueError(f"unknown CI test group: {group}")


def run_group(group: str, *, case: str | None = None) -> int:
    errors = inventory_errors()
    if errors:
        raise RuntimeError("; ".join(errors))
    command = [sys.executable, "-m", "pytest", "-q", *pytest_args(group, case=case)]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    subparsers.add_parser("list-migrations")
    run = subparsers.add_parser("run")
    run.add_argument("group", choices=("unit", *DATABASE_GROUPS, "migration"))
    run.add_argument("--case", choices=tuple(MIGRATION_CASES))
    args = parser.parse_args()

    if args.command == "validate":
        errors = inventory_errors()
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
        return 0
    if args.command == "list-migrations":
        print(json.dumps(sorted(MIGRATION_CASES)))
        return 0
    return run_group(args.group, case=args.case)


if __name__ == "__main__":
    sys.exit(main())
