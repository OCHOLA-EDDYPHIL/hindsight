"""Run shared local and hosted live-acceptance stages."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
from typing import Any
from urllib.parse import parse_qs, urlsplit

import psycopg

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCAL_DATABASE_HOSTS = {"localhost", "127.0.0.1", "::1"}
PROVIDER_SELECTORS = (
    "tests/test_embeddings.py::test_live_gemini_embedding_provider_ranks_low_overlap_paraphrase",
    "tests/test_embeddings.py::test_live_gemini_embedding_provider_ranks_frozen_pilot_reference_lessons",
    "tests/test_reasoning.py::test_live_gemini_reasoning_provider",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("verify-providers")

    local = subparsers.add_parser("local-pilot")
    local.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    local.add_argument("--max-distance", type=float, default=0.35)
    local.add_argument("--report", type=pathlib.Path, required=True)
    local.add_argument("--code-sha")

    hosted = subparsers.add_parser("hosted-benchmark")
    hosted.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    hosted.add_argument("--max-distance", type=float, default=0.35)
    hosted.add_argument("--report-dir", type=pathlib.Path, required=True)
    hosted.add_argument("--summary-path", type=pathlib.Path)

    args = parser.parse_args()
    if args.command == "verify-providers":
        _verify_providers()
    elif args.command == "local-pilot":
        _run_local_pilot(args)
    else:
        _run_hosted_benchmark(args)


def _verify_providers() -> None:
    _require_gemini_credentials()
    env = dict(os.environ)
    env.update(
        {
            "RUN_LIVE_GEMINI_EMBEDDINGS": "1",
            "RUN_LIVE_GEMINI_REASONING": "1",
        }
    )
    _run([sys.executable, "-m", "pytest", "-q", *PROVIDER_SELECTORS], env=env)


def _run_local_pilot(args: argparse.Namespace) -> None:
    database_url = _required_database_url(args.database_url)
    _require_local_database(database_url)
    _require_gemini_credentials()
    code_sha = args.code_sha or _code_sha()
    env = _live_environment(database_url=database_url, code_sha=code_sha)
    args.report.parent.mkdir(parents=True, exist_ok=True)

    _run([sys.executable, "scripts/migrate.py"], env=env)
    _run([sys.executable, "scripts/initialize_agent_storage.py"], env=env)
    _run(
        [
            sys.executable,
            "scripts/reembed_memories.py",
            "--max-distance",
            str(args.max_distance),
        ],
        env=env,
    )
    _run(
        [
            sys.executable,
            "scripts/run_learning_benchmark.py",
            "pilot",
            "--repetitions",
            "2",
            "--max-distance",
            str(args.max_distance),
        ],
        env=env,
        stdout_path=args.report,
    )
    report = _load_report(args.report)
    _validate_local_pilot(
        database_url=database_url,
        experiment_id=_experiment_id(report),
    )


def _run_hosted_benchmark(args: argparse.Namespace) -> None:
    database_url = _required_database_url(args.database_url)
    _require_hosted_database(database_url)
    _require_gemini_credentials()
    args.report_dir.mkdir(parents=True, exist_ok=True)
    env = _live_environment(
        database_url=database_url,
        code_sha=_required_code_sha(),
    )
    pilot_path = args.report_dir / "pilot.json"
    preregistration_path = args.report_dir / "preregistration.json"
    confirmation_path = args.report_dir / "confirmation.json"

    _run(
        [
            sys.executable,
            "scripts/run_learning_benchmark.py",
            "pilot",
            "--repetitions",
            "2",
            "--max-distance",
            str(args.max_distance),
        ],
        env=env,
        stdout_path=pilot_path,
    )
    pilot_id = _experiment_id(_load_report(pilot_path))
    _run(
        [
            sys.executable,
            "scripts/run_learning_benchmark.py",
            "preregister",
            "--pilot-experiment-id",
            pilot_id,
        ],
        env=env,
        stdout_path=preregistration_path,
    )
    _run(
        [
            sys.executable,
            "scripts/run_learning_benchmark.py",
            "confirmation",
            "--pilot-experiment-id",
            pilot_id,
        ],
        env=env,
        stdout_path=confirmation_path,
    )
    confirmation = _load_report(confirmation_path)
    _require_confirmation_gates(confirmation)
    if args.summary_path is not None:
        _append_confirmation_summary(args.summary_path, confirmation)


def _run(
    command: list[str],
    *,
    env: dict[str, str],
    stdout_path: pathlib.Path | None = None,
) -> None:
    if stdout_path is None:
        subprocess.run(command, cwd=ROOT, env=env, check=True)
        return
    with stdout_path.open("w", encoding="utf-8") as output:
        subprocess.run(command, cwd=ROOT, env=env, check=True, stdout=output)


def _required_database_url(value: str | None) -> str:
    if not value:
        raise ValueError("DATABASE_URL or --database-url is required")
    return value


def _require_local_database(database_url: str) -> None:
    parts = urlsplit(database_url)
    database_name = parts.path.lstrip("/")
    query = parse_qs(parts.query)
    if (
        parts.scheme not in {"postgres", "postgresql"}
        or parts.hostname not in LOCAL_DATABASE_HOSTS
        or database_name in {"", "defaultdb", "postgres"}
        or query.get("sslmode") != ["disable"]
    ):
        raise ValueError(
            "local-pilot requires a named loopback database with sslmode=disable"
        )
    try:
        with psycopg.connect(database_url) as conn:
            migrated = conn.execute(
                """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'public'
                            AND table_name = 'schema_migrations'
                    )
                """
            ).fetchone()[0]
    except psycopg.errors.InvalidCatalogName:
        return
    if migrated:
        raise ValueError("local-pilot requires a fresh database without migrations")


def _require_hosted_database(database_url: str) -> None:
    parts = urlsplit(database_url)
    if parts.hostname in LOCAL_DATABASE_HOSTS:
        raise ValueError("hosted-benchmark refuses loopback databases")


def _require_gemini_credentials() -> None:
    if not any(
        (os.environ.get(name) or "").strip()
        for name in ("GEMINI_API_KEYS", "GEMINI_API_KEY")
    ):
        raise ValueError("Gemini credentials must already be loaded into the environment")


def _live_environment(*, database_url: str, code_sha: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": database_url,
            "EMBEDDING_PROVIDER": "gemini",
            "LLM_PROVIDER": "gemini",
            "HINDSIGHT_BENCHMARK_CODE_SHA": code_sha,
        }
    )
    return env


def _code_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _required_code_sha() -> str:
    value = (os.environ.get("HINDSIGHT_BENCHMARK_CODE_SHA") or "").strip()
    if not value:
        raise ValueError("HINDSIGHT_BENCHMARK_CODE_SHA is required")
    return value


def _load_report(path: pathlib.Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"acceptance report is empty: {path.name}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload:
        raise RuntimeError(f"acceptance report is invalid: {path.name}")
    return payload


def _experiment_id(report: dict[str, Any]) -> str:
    value = str(report.get("experiment_id") or "").strip()
    if not value:
        raise RuntimeError("benchmark report has no experiment_id")
    return value


def _validate_local_pilot(*, database_url: str, experiment_id: str) -> None:
    with psycopg.connect(database_url) as conn:
        experiment = conn.execute(
            """
                SELECT experiment_kind, status
                FROM benchmark_experiments WHERE id = %s
            """,
            (experiment_id,),
        ).fetchone()
        preparations = conn.execute(
            """
                SELECT count(*), count(*) FILTER (WHERE status = 'completed')
                FROM benchmark_variant_preparations WHERE experiment_id = %s
            """,
            (experiment_id,),
        ).fetchone()
    if experiment != ("pilot", "completed") or preparations != (6, 6):
        raise RuntimeError("local pilot did not complete all frozen preparations")


def _require_confirmation_gates(report: dict[str, Any]) -> None:
    gates = report.get("gates")
    if (
        report.get("claim_authorized") is not True
        or not isinstance(gates, dict)
        or not gates
        or not all(value is True for value in gates.values())
    ):
        raise RuntimeError("confirmation did not authorize the semantic-learning claim")


def _append_confirmation_summary(path: pathlib.Path, report: dict[str, Any]) -> None:
    gates = dict(report["gates"])
    lines = [
        "### Preregistered live benchmark",
        "",
        f"- Experiment: `{report.get('experiment_id')}`",
        f"- Trace digest: `{report.get('raw_trace_digest')}`",
        f"- Claim authorized: `{report.get('claim_authorized') is True}`",
        "",
        "| Gate | Result |",
        "| --- | --- |",
        *(f"| {name} | {'pass' if value is True else 'fail'} |" for name, value in sorted(gates.items())),
    ]
    with path.open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
