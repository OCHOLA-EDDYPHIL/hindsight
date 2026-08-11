"""Run historical migration cases against isolated databases on one server."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ci_test_groups import MIGRATION_CASES  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
ROLE_SENSITIVE_CASES = ("agent_runtime_roles", "populated_roles")
PARALLEL_CASES = tuple(case for case in MIGRATION_CASES if case not in ROLE_SENSITIVE_CASES)
EVIDENCE_SCHEMA = "hindsight.migration_compatibility_history.v1"
SOURCE_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


def _identifier(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return cleaned[:24] or "local"


def case_database_name(case: str, run_token: str) -> str:
    return f"migration_{_identifier(run_token)}_{_identifier(case)}"[:63]


def database_url(base_url: str, database: str) -> str:
    parsed = urlsplit(base_url)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database}", parsed.query, parsed.fragment))


def create_case_databases(base_url: str, cases: tuple[str, ...], run_token: str) -> None:
    admin_url = database_url(base_url, "defaultdb")
    with psycopg.connect(admin_url, autocommit=True) as connection:
        for case in cases:
            name = case_database_name(case, run_token)
            connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} CASCADE").format(sql.Identifier(name)))
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def run_case(case: str, *, base_url: str, run_token: str) -> tuple[str, int, str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url(base_url, case_database_name(case, run_token))
    result = subprocess.run(
        [sys.executable, "scripts/ci_test_groups.py", "run", "migration", "--case", case],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    output = "".join((result.stdout, result.stderr))
    return case, result.returncode, output


def run_role_sensitive_cases(*, base_url: str, run_token: str) -> list[tuple[str, int, str]]:
    return [
        run_case(case, base_url=base_url, run_token=run_token)
        for case in ROLE_SENSITIVE_CASES
    ]


def emit_result(result: tuple[str, int, str]) -> None:
    case, _returncode, output = result
    print(f"--- migration case: {case} ---")
    print(output, end="" if output.endswith("\n") else "\n", flush=True)


def _write_evidence(output: Path, document: dict[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    temporary.replace(output)


def run_all(
    *,
    base_url: str,
    run_token: str,
    workers: int,
    evidence_output: Path | None = None,
    source_revision: str = "",
    workflow_run_id: int = 0,
    workflow_run_attempt: int = 0,
) -> int:
    cases = tuple(MIGRATION_CASES)
    create_case_databases(base_url, cases, run_token)
    results: list[tuple[str, int, str]] = []
    with ThreadPoolExecutor(max_workers=workers + 1) as executor:
        futures = {
            executor.submit(run_case, case, base_url=base_url, run_token=run_token): case
            for case in PARALLEL_CASES
        }
        role_future = executor.submit(
            run_role_sensitive_cases,
            base_url=base_url,
            run_token=run_token,
        )
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            emit_result(result)
        role_results = role_future.result()
        results.extend(role_results)
        for result in role_results:
            emit_result(result)

    failures: list[str] = []
    for case, returncode, _output in results:
        if returncode:
            failures.append(case)
    return_code = int(bool(failures))
    if failures:
        print(f"failed migration cases: {', '.join(sorted(failures))}", file=sys.stderr)
    if evidence_output is not None:
        if SOURCE_SHA_PATTERN.fullmatch(source_revision) is None:
            raise ValueError("source revision must be a full lowercase Git SHA")
        if workflow_run_id < 1 or workflow_run_attempt < 1:
            raise ValueError("workflow run id and attempt must be positive integers")
        by_name = {case: returncode for case, returncode, _output in results}
        if set(by_name) != set(MIGRATION_CASES) or len(results) != len(MIGRATION_CASES):
            raise RuntimeError("migration result set is incomplete or duplicated")
        _write_evidence(
            evidence_output,
            {
                "schema_version": EVIDENCE_SCHEMA,
                "status": "failed" if return_code else "passed",
                "source_revision": source_revision,
                "workflow_run": {
                    "id": workflow_run_id,
                    "attempt": workflow_run_attempt,
                },
                "cases": [
                    {
                        "name": case,
                        "return_code": by_name[case],
                        "succeeded": by_name[case] == 0,
                    }
                    for case in sorted(MIGRATION_CASES)
                ],
            },
        )
    return return_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument(
        "--run-token",
        default=f"{os.environ.get('GITHUB_RUN_ID', 'local')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}",
    )
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-revision", default=os.environ.get("SOURCE_SHA", ""))
    parser.add_argument(
        "--workflow-run-id",
        type=int,
        default=int(os.environ.get("GITHUB_RUN_ID", "0")),
    )
    parser.add_argument(
        "--workflow-run-attempt",
        type=int,
        default=int(os.environ.get("GITHUB_RUN_ATTEMPT", "0")),
    )
    args = parser.parse_args()
    if not args.base_url:
        parser.error("--base-url or DATABASE_URL is required")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    return run_all(
        base_url=args.base_url,
        run_token=args.run_token,
        workers=args.workers,
        evidence_output=args.output,
        source_revision=args.source_revision,
        workflow_run_id=args.workflow_run_id,
        workflow_run_attempt=args.workflow_run_attempt,
    )


if __name__ == "__main__":
    raise SystemExit(main())
