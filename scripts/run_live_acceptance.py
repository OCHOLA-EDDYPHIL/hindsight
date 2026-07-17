"""Run shared local and hosted live-acceptance stages."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import secrets
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib import request
from urllib.parse import parse_qs, urlsplit, urlunsplit
from uuid import uuid4
from xml.etree import ElementTree

import psycopg
from psycopg import sql

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCAL_DATABASE_HOSTS = {"localhost", "127.0.0.1", "::1"}
BENCHMARK_MAX_DISTANCE = 0.35
ACCEPTANCE_ARTIFACT_DIR_ENV = "HINDSIGHT_ACCEPTANCE_ARTIFACT_DIR"
PILOT_EMBEDDING_SELECTOR = (
    "tests/test_embeddings.py::"
    "test_live_gemini_embedding_provider_ranks_frozen_pilot_reference_lessons"
)
PROVIDER_SANITY_SELECTORS = (
    "tests/test_embeddings.py::test_live_gemini_embedding_provider_ranks_low_overlap_paraphrase",
    "tests/test_reasoning.py::test_live_gemini_reasoning_provider",
)
SEMANTIC_RETRIEVAL_SELECTORS = (
    "tests/test_live_semantic_acceptance.py::"
    "test_live_gemini_database_retrieval_discriminates_paraphrase_and_no_match",
    "tests/test_live_semantic_acceptance.py::"
    "test_live_gemini_cutoff_generalizes_across_calibration_mechanisms",
)
DIRECT_CONSOLIDATION_SELECTOR = (
    "tests/test_live_semantic_acceptance.py::"
    "test_live_gemini_consolidation_publishes_cited_retrievable_lesson"
)
LOCAL_SEMANTIC_SELECTORS = (*SEMANTIC_RETRIEVAL_SELECTORS, DIRECT_CONSOLIDATION_SELECTOR)
RESILIENCE_SELECTORS = (
    "tests/test_migrations_and_roles.py",
    "tests/test_agent.py::"
    "test_preinitialized_agent_storage_supports_start_and_resume_without_create_privilege",
    "tests/test_run_dispatch.py",
    "tests/test_run_attempts.py",
    "tests/test_operation_retries.py",
    "tests/test_consolidation.py",
    "tests/test_worker.py",
    "tests/test_realtime.py",
)
MANAGED_CONSOLIDATION_SELECTOR = (
    "tests/test_hosted_acceptance.py::"
    "test_resolved_transition_reaches_managed_changefeed_worker_and_cited_lesson"
)
WEBSOCKET_RECONNECT_SELECTOR = (
    "tests/test_hosted_acceptance.py::"
    "test_websocket_requires_resubscribe_after_reconnect_and_honors_unsubscribe"
)
SIGNATURE_BROWSER_CONTRACT_SELECTOR = (
    "tests/test_browser_ui.py::test_operator_can_run_and_explain_signature_workflow"
)
HISTORICAL_NAMESPACE_BROWSER_CONTRACT_SELECTOR = (
    "tests/test_browser_ui.py::"
    "test_review_required_memory_renders_as_active_in_its_historical_snapshot"
)
SHARED_BROWSER_CONTRACT_SELECTORS = (
    SIGNATURE_BROWSER_CONTRACT_SELECTOR,
    HISTORICAL_NAMESPACE_BROWSER_CONTRACT_SELECTOR,
)
HOSTED_ONLY_INFRASTRUCTURE_SELECTORS_BY_PHASE = {
    "consolidation": (MANAGED_CONSOLIDATION_SELECTOR,),
    "browser": (WEBSOCKET_RECONNECT_SELECTOR,),
}
HOSTED_ONLY_INFRASTRUCTURE_SELECTORS = tuple(
    selector
    for selectors in HOSTED_ONLY_INFRASTRUCTURE_SELECTORS_BY_PHASE.values()
    for selector in selectors
)
LOCAL_BROWSER_PRODUCT_SELECTORS = (
    "tests/test_api.py",
    "tests/test_dashboard.py",
    "tests/test_queueing.py",
    *SHARED_BROWSER_CONTRACT_SELECTORS,
)
HOSTED_BROWSER_PRODUCT_SELECTORS = (
    *SHARED_BROWSER_CONTRACT_SELECTORS,
    *HOSTED_ONLY_INFRASTRUCTURE_SELECTORS_BY_PHASE["browser"],
)
WORKER_PRODUCT_SELECTORS = (
    "tests/test_hosted_acceptance.py::"
    "test_scheduled_dispatch_reclaims_expired_attempt_and_finalizes_dlq",
)
ROLE_PRODUCT_SELECTORS = (
    "tests/test_hosted_database_roles.py::"
    "test_hosted_runtime_database_identities_are_distinct_and_restricted",
)
HOSTED_PHASE_SELECTORS = {
    "semantic": SEMANTIC_RETRIEVAL_SELECTORS,
    "consolidation": HOSTED_ONLY_INFRASTRUCTURE_SELECTORS_BY_PHASE["consolidation"],
    "worker": WORKER_PRODUCT_SELECTORS,
    "browser": HOSTED_BROWSER_PRODUCT_SELECTORS,
    "roles": ROLE_PRODUCT_SELECTORS,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    product = subparsers.add_parser("local-product-full")
    product.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    product.add_argument("--base-url", default="http://127.0.0.1:8766")

    hosted_product = subparsers.add_parser("hosted-product")
    hosted_product.add_argument(
        "--phase",
        choices=("providers", *HOSTED_PHASE_SELECTORS),
        required=True,
    )
    hosted_product.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
    )

    local_pilot = subparsers.add_parser("learning-pilot")
    _add_local_benchmark_arguments(local_pilot)

    learning_full = subparsers.add_parser("learning-full")
    _add_local_benchmark_arguments(learning_full)
    learning_full.add_argument(
        "--database-scope",
        choices=("local", "hosted"),
        required=True,
    )
    learning_full.add_argument("--summary-path", type=pathlib.Path)

    args = parser.parse_args()
    if args.command == "local-product-full":
        _run_local_product_full(args)
    elif args.command == "hosted-product":
        _run_hosted_product(args)
    elif args.command == "learning-pilot":
        _preflight_local_learning(args)
        _verify_learning_providers()
        _run_local_benchmark(args, include_confirmation=False)
    else:
        if args.database_scope == "local":
            _preflight_local_learning(args)
            _verify_learning_providers()
            _run_local_benchmark(args, include_confirmation=True)
        else:
            _verify_learning_providers()
            _run_hosted_benchmark(args)


def _add_local_benchmark_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--max-distance", type=float, default=BENCHMARK_MAX_DISTANCE)
    parser.add_argument("--report-dir", type=pathlib.Path, required=True)
    parser.add_argument("--code-sha")


def _preflight_local_learning(args: argparse.Namespace) -> None:
    _require_fixed_max_distance(args.max_distance)
    _validate_local_url(_required_database_url(args.database_url))
    _require_local_report_path(args.report_dir)
    _require_local_code_sha(args.code_sha)


def _verify_product_providers() -> None:
    _require_gemini_credentials()
    env = dict(os.environ)
    env.update(
        {
            "RUN_LIVE_GEMINI_EMBEDDINGS": "1",
            "RUN_LIVE_GEMINI_REASONING": "1",
        }
    )
    _add_single_gemini_key(env)
    artifact_dir = _acceptance_artifact_dir("providers")
    _run_strict_pytest(
        PROVIDER_SANITY_SELECTORS,
        env=env,
        phase="providers",
        artifact_dir=artifact_dir,
    )


def _verify_learning_providers() -> None:
    _require_gemini_credentials()
    env = dict(os.environ)
    env.update(
        {
            "RUN_LIVE_GEMINI_EMBEDDINGS": "1",
            "RUN_LIVE_GEMINI_REASONING": "1",
        }
    )
    _add_single_gemini_key(env)
    for _ in range(2):
        _run([sys.executable, "-m", "pytest", "-q", PILOT_EMBEDDING_SELECTOR], env=env)


def _verify_semantic(args: argparse.Namespace) -> None:
    database_url = _required_database_url(args.database_url)
    if args.database_scope == "local":
        _validate_local_url(database_url)
        selectors = LOCAL_SEMANTIC_SELECTORS
    else:
        _require_hosted_database(database_url)
        selectors = SEMANTIC_RETRIEVAL_SELECTORS
    _require_gemini_credentials()
    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": database_url,
            "EMBEDDING_PROVIDER": "gemini",
            "LLM_PROVIDER": "gemini",
            "RUN_LIVE_GEMINI_ACCEPTANCE": "1",
        }
    )
    phase = f"semantic-{args.database_scope}"
    artifact_dir = _acceptance_artifact_dir(phase)
    _run_strict_pytest(
        selectors,
        env=env,
        phase=phase,
        artifact_dir=artifact_dir,
    )


def _verify_resilience(args: argparse.Namespace) -> None:
    database_url = _required_database_url(args.database_url)
    _validate_local_url(database_url)
    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": database_url,
            "EMBEDDING_PROVIDER": "deterministic",
            "LLM_PROVIDER": "deterministic",
        }
    )
    artifact_dir = _acceptance_artifact_dir("resilience")
    _run_strict_pytest(
        RESILIENCE_SELECTORS,
        env=env,
        phase="resilience",
        artifact_dir=artifact_dir,
    )


def _run_local_product_full(args: argparse.Namespace) -> None:
    database_url = _required_database_url(args.database_url)
    parts, database_name = _validate_local_url(database_url)
    run_token = uuid4().hex[:12]
    semantic_url = _local_database_url(parts, f"{database_name}_{run_token}_semantic")
    resilience_url = _local_database_url(parts, f"{database_name}_{run_token}_resilience")
    browser_url = _local_database_url(parts, f"{database_name}_{run_token}_browser")
    for url in (semantic_url, resilience_url, browser_url):
        _create_local_database(url)

    _verify_product_providers()
    semantic_env = _product_environment(semantic_url, live=True)
    _initialize_product_database(semantic_env, configure_embeddings=True)
    _verify_semantic(
        argparse.Namespace(database_url=semantic_url, database_scope="local")
    )

    resilience_env = _product_environment(resilience_url, live=False)
    _initialize_product_database(resilience_env, configure_embeddings=False)
    _verify_resilience(argparse.Namespace(database_url=resilience_url))

    browser_env = _product_environment(browser_url, live=True)
    _initialize_product_database(browser_env, configure_embeddings=True)
    _run_local_browser_product(
        database_url=browser_url,
        base_url=args.base_url,
    )


def _initialize_product_database(
    env: dict[str, str], *, configure_embeddings: bool
) -> None:
    _run([sys.executable, "scripts/migrate.py"], env=env)
    _run([sys.executable, "scripts/initialize_agent_storage.py"], env=env)
    if configure_embeddings:
        _run(
            [
                sys.executable,
                "scripts/reembed_memories.py",
                "--max-distance",
                str(BENCHMARK_MAX_DISTANCE),
            ],
            env=env,
        )


def _run_local_browser_product(*, database_url: str, base_url: str) -> None:
    base_url = str(base_url).rstrip("/")
    parts = urlsplit(base_url)
    if parts.scheme != "http" or parts.hostname not in LOCAL_DATABASE_HOSTS or parts.port is None:
        raise ValueError("local-product-full requires an explicit loopback HTTP port")
    _require_gemini_credentials()
    token = secrets.token_hex(32)
    env = _product_environment(database_url, live=True)
    for name in (
        "RUN_HOSTED_ACCEPTANCE",
        "HOSTED_API_URL",
        "HINDSIGHT_WEBSOCKET_URL",
        "HINDSIGHT_DEPLOY_DATABASE_URL_PARAM",
        "HINDSIGHT_API_DATABASE_URL_PARAM",
        "HINDSIGHT_WORKER_DATABASE_URL_PARAM",
    ):
        env.pop(name, None)
    env.update(
        {
            "DATABASE_URL": database_url,
            "HINDSIGHT_DATABASE_URL_PARAM": "",
            "HINDSIGHT_GEMINI_API_KEY_PARAM": "",
            "HINDSIGHT_GEMINI_API_KEYS_PARAM": "",
            "HINDSIGHT_FUNCTION_AUTH_TOKEN": token,
            "HINDSIGHT_INLINE_WORKER": "1",
            "HINDSIGHT_RUN_DLQ_ARN": "local:sqs:hindsight-run-dlq",
            "HINDSIGHT_SECURE_COOKIES": "0",
            "HINDSIGHT_ALLOWED_ORIGINS": base_url,
            "HINDSIGHT_BROWSER_BASE_URL": base_url,
            "HINDSIGHT_BROWSER_OPERATOR_TOKEN": token,
        }
    )
    artifact_dir = _acceptance_artifact_dir("local-browser")
    env[ACCEPTANCE_ARTIFACT_DIR_ENV] = str(artifact_dir)
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "hindsight.api:app",
            "--host",
            str(parts.hostname),
            "--port",
            str(parts.port),
        ],
        cwd=ROOT,
        env=env,
    )
    try:
        _wait_for_http_ready(f"{base_url}/v1/health/ready", server=server)
        _run_strict_pytest(
            LOCAL_BROWSER_PRODUCT_SELECTORS,
            env=env,
            phase="local-browser",
            artifact_dir=artifact_dir,
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=15)


def _wait_for_http_ready(url: str, *, server: subprocess.Popen[Any]) -> None:
    for _ in range(60):
        if server.poll() is not None:
            raise RuntimeError("local product server exited before readiness")
        try:
            with request.urlopen(url, timeout=2) as response:  # noqa: S310 - loopback URL
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError("local product server did not become ready")


def _run_hosted_product(args: argparse.Namespace) -> None:
    if args.phase == "providers":
        _verify_product_providers()
        return
    env = dict(os.environ)
    env["HINDSIGHT_ACCEPTANCE_PHASE_ID"] = str(uuid4())
    env["RUN_HOSTED_ACCEPTANCE"] = "1"
    selectors = HOSTED_PHASE_SELECTORS[args.phase]
    if args.phase == "roles":
        for name in (
            "HINDSIGHT_DEPLOY_DATABASE_URL_PARAM",
            "HINDSIGHT_API_DATABASE_URL_PARAM",
            "HINDSIGHT_WORKER_DATABASE_URL_PARAM",
            "AWS_REGION",
        ):
            _required_env(name)
        _run_hosted_pytest(selectors, env=env, phase=args.phase)
        return

    database_url = _required_database_url(args.database_url)
    _require_hosted_database(database_url)
    env["DATABASE_URL"] = database_url
    if args.phase in {"semantic", "consolidation", "browser"}:
        _require_gemini_credentials()
        env["RUN_LIVE_GEMINI_ACCEPTANCE"] = "1"
    if args.phase == "consolidation":
        _required_https_env("HOSTED_API_URL")
        _required_env("HINDSIGHT_BROWSER_OPERATOR_TOKEN")
        _verify_changefeed(env)
    elif args.phase == "worker":
        for name in (
            "HINDSIGHT_ACCEPTANCE_RUN_ATTEMPT_LEASE_SECONDS",
            "HINDSIGHT_ACCEPTANCE_QUEUE_VISIBILITY_SECONDS",
            "HINDSIGHT_ACCEPTANCE_RUN_MAX_ATTEMPTS",
            "HINDSIGHT_ACCEPTANCE_SCHEDULER_SECONDS",
        ):
            _required_positive_int_env(name)
    elif args.phase == "browser":
        _verify_hosted_endpoints()
        _required_env("HINDSIGHT_BROWSER_OPERATOR_TOKEN")
        _required_env("HINDSIGHT_CHANGEFEED_AUTH_TOKEN")
        _verify_changefeed(env)
    _run_hosted_pytest(selectors, env=env, phase=args.phase)


def _verify_changefeed(env: dict[str, str]) -> None:
    _run([sys.executable, "scripts/configure_changefeed.py", "status"], env=env)


def _run_hosted_pytest(
    selectors: tuple[str, ...], *, env: dict[str, str], phase: str
) -> None:
    artifact_dir = _acceptance_artifact_dir(phase)
    env[ACCEPTANCE_ARTIFACT_DIR_ENV] = str(artifact_dir)
    _run_strict_pytest(
        selectors,
        env=env,
        phase=phase,
        artifact_dir=artifact_dir,
    )


def _acceptance_artifact_dir(phase: str) -> pathlib.Path:
    configured = (os.environ.get(ACCEPTANCE_ARTIFACT_DIR_ENV) or "").strip()
    if configured:
        directory = pathlib.Path(configured).resolve()
        if directory == ROOT or ROOT in directory.parents:
            raise ValueError("acceptance artifacts must be outside the repository")
        directory.mkdir(parents=True, exist_ok=True)
    else:
        directory = pathlib.Path(tempfile.mkdtemp(prefix=f"hindsight-{phase}-"))
    print(f"{phase} acceptance artifacts: {directory}")
    return directory


def _run_strict_pytest(
    selectors: tuple[str, ...],
    *,
    env: dict[str, str],
    phase: str,
    artifact_dir: pathlib.Path,
) -> None:
    report = artifact_dir / "pytest.xml"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-x",
        f"--junitxml={report}",
        *selectors,
    ]
    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    if not report.is_file() or report.stat().st_size == 0:
        raise RuntimeError(f"{phase} acceptance did not produce JUnit evidence")
    root = ElementTree.parse(report).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    tests = sum(int(suite.attrib.get("tests", "0")) for suite in suites)
    skipped = sum(int(suite.attrib.get("skipped", "0")) for suite in suites)
    if completed.returncode:
        raise subprocess.CalledProcessError(completed.returncode, command)
    if tests < 1 or skipped:
        raise RuntimeError(f"{phase} acceptance ran {tests} tests with {skipped} skipped")


def _verify_hosted_endpoints() -> None:
    ui_url = _required_https_env("HINDSIGHT_BROWSER_BASE_URL").rstrip("/")
    api_url = _required_https_env("HOSTED_API_URL").rstrip("/")
    websocket_url = _required_env("HINDSIGHT_WEBSOCKET_URL")
    if not websocket_url.startswith("wss://"):
        raise ValueError("hosted product WebSocket endpoint must use WSS")
    for url in (f"{ui_url}/v1/health/ready", f"{api_url}/v1/health/ready"):
        with request.urlopen(url, timeout=30) as response:  # noqa: S310 - guarded HTTPS URL
            if response.status != 200:
                raise RuntimeError(f"hosted product endpoint is not ready: {url}")


def _run_local_benchmark(args: argparse.Namespace, *, include_confirmation: bool) -> None:
    database_url = _required_database_url(args.database_url)
    _require_fixed_max_distance(args.max_distance)
    _validate_local_url(database_url)
    _require_gemini_credentials()
    code_sha = _require_local_code_sha(args.code_sha)
    _require_local_report_path(args.report_dir)
    _create_local_database(database_url)
    report_dir = _new_report_dir(args.report_dir)
    env = _live_environment(database_url=database_url, code_sha=code_sha)

    _run([sys.executable, "scripts/migrate.py"], env=env)
    _run([sys.executable, "scripts/initialize_agent_storage.py"], env=env)
    _run(
        [
            sys.executable,
            "scripts/reembed_memories.py",
            "--max-distance",
            str(BENCHMARK_MAX_DISTANCE),
        ],
        env=env,
    )
    if include_confirmation:
        _run_benchmark_sequence(
            database_url=database_url,
            env=env,
            report_dir=report_dir,
        )
    else:
        _run_pilot_and_preregister(
            database_url=database_url,
            env=env,
            report_dir=report_dir,
        )


def _run_hosted_benchmark(args: argparse.Namespace) -> None:
    database_url = _required_database_url(args.database_url)
    _require_hosted_database(database_url)
    _require_fixed_max_distance(args.max_distance)
    _require_gemini_credentials()
    report_dir = _new_report_dir(args.report_dir)
    env = _live_environment(
        database_url=database_url,
        code_sha=_required_code_sha(),
    )
    confirmation = _run_benchmark_sequence(
        database_url=database_url,
        env=env,
        report_dir=report_dir,
    )
    if args.summary_path is not None:
        _append_confirmation_summary(args.summary_path, confirmation)


def _run_benchmark_sequence(
    *, database_url: str, env: dict[str, str], report_dir: pathlib.Path
) -> dict[str, Any]:
    pilot, _preregistration = _run_pilot_and_preregister(
        database_url=database_url,
        env=env,
        report_dir=report_dir,
    )
    pilot_id = _experiment_id(pilot)
    confirmation_path = report_dir / "confirmation.json"
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
    confirmation_id = _experiment_id(confirmation)
    _validate_experiment(
        database_url=database_url,
        experiment_id=confirmation_id,
        experiment_kind="confirmation",
        expected_preparations=12,
        expected_trials=72,
    )
    _require_confirmation_gates(confirmation)
    return confirmation


def _run_pilot_and_preregister(
    *, database_url: str, env: dict[str, str], report_dir: pathlib.Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    pilot = _run_pilot(database_url=database_url, env=env, report_dir=report_dir)
    pilot_id = _experiment_id(pilot)
    _validate_experiment(
        database_url=database_url,
        experiment_id=pilot_id,
        experiment_kind="pilot",
        expected_preparations=6,
        expected_trials=36,
    )
    preregistration_path = report_dir / "preregistration.json"
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
    preregistration = _load_report(preregistration_path)
    _require_preregistration(preregistration, pilot_id=pilot_id)
    return pilot, preregistration


def _run_pilot(
    *, database_url: str, env: dict[str, str], report_dir: pathlib.Path
) -> dict[str, Any]:
    pilot_path = report_dir / "pilot.json"
    _run(
        [
            sys.executable,
            "scripts/run_learning_benchmark.py",
            "pilot",
            "--repetitions",
            "2",
            "--max-distance",
            str(BENCHMARK_MAX_DISTANCE),
        ],
        env=env,
        stdout_path=pilot_path,
    )
    pilot = _load_report(pilot_path)
    _require_report_identity(pilot, experiment_kind="pilot")
    return pilot


def _run(
    command: list[str],
    *,
    env: dict[str, str],
    stdout_path: pathlib.Path | None = None,
) -> None:
    if stdout_path is None:
        subprocess.run(command, cwd=ROOT, env=env, check=True)
        return
    with stdout_path.open("x", encoding="utf-8") as output:
        subprocess.run(command, cwd=ROOT, env=env, check=True, stdout=output)


def _required_database_url(value: str | None) -> str:
    if not value:
        raise ValueError("DATABASE_URL or --database-url is required")
    return value


def _validate_local_url(database_url: str):
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
            "local acceptance requires a named loopback database with sslmode=disable"
        )
    return parts, database_name


def _local_database_url(parts: Any, database_name: str) -> str:
    return urlunsplit(parts._replace(path=f"/{database_name}"))


def _create_local_database(database_url: str) -> None:
    parts, database_name = _validate_local_url(database_url)
    admin_url = urlunsplit(parts._replace(path="/defaultdb"))
    with psycopg.connect(admin_url, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT count(*) FROM [SHOW DATABASES] WHERE database_name = %s",
            (database_name,),
        ).fetchone()[0]
        if exists:
            raise ValueError("local acceptance requires a new database name")
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))


def _require_hosted_database(database_url: str) -> None:
    parts = urlsplit(database_url)
    if parts.hostname in LOCAL_DATABASE_HOSTS:
        raise ValueError("hosted acceptance refuses loopback databases")


def _require_fixed_max_distance(value: float) -> None:
    if value != BENCHMARK_MAX_DISTANCE:
        raise ValueError("live acceptance fixes max distance at 0.35")


def _new_report_dir(path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()
    return path


def _require_local_report_path(path: pathlib.Path) -> None:
    resolved = path.resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise ValueError("local acceptance reports must be outside the repository")
    if resolved.exists():
        raise ValueError("local acceptance requires a new report directory")


def _require_gemini_credentials() -> None:
    if not any(
        (os.environ.get(name) or "").strip()
        for name in ("GEMINI_API_KEYS", "GEMINI_API_KEY")
    ):
        raise ValueError("Gemini credentials must already be loaded into the environment")


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _required_https_env(name: str) -> str:
    value = _required_env(name)
    if not value.startswith("https://"):
        raise ValueError(f"{name} must use HTTPS")
    return value


def _required_positive_int_env(name: str) -> int:
    value = _required_env(name)
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _product_environment(database_url: str, *, live: bool) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": database_url,
            "EMBEDDING_PROVIDER": "gemini" if live else "deterministic",
            "LLM_PROVIDER": "gemini" if live else "deterministic",
            "HINDSIGHT_DATABASE_URL_PARAM": "",
            "HINDSIGHT_GEMINI_API_KEY_PARAM": "",
            "HINDSIGHT_GEMINI_API_KEYS_PARAM": "",
        }
    )
    return env


def _add_single_gemini_key(env: dict[str, str]) -> None:
    if (env.get("GEMINI_API_KEY") or "").strip():
        return
    from hindsight.gemini import parse_gemini_credentials

    credentials = parse_gemini_credentials(env)
    if not credentials:
        raise ValueError("Gemini credential document contains no usable key")
    env["GEMINI_API_KEY"] = credentials[0].api_key


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


def _require_local_code_sha(value: str | None) -> str:
    head = _code_sha()
    resolved = (value or head).strip()
    if resolved != head:
        raise ValueError("local benchmark code SHA must equal HEAD")
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        raise ValueError("local benchmark requires a clean exact-HEAD worktree")
    return resolved


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


def _require_report_identity(report: dict[str, Any], *, experiment_kind: str) -> None:
    if report.get("experiment_kind") != experiment_kind or report.get("status") != "completed":
        raise RuntimeError(f"{experiment_kind} report is not completed")
    for field in ("raw_trace_digest", "claim_evidence_digest"):
        if not str(report.get(field) or "").strip():
            raise RuntimeError(f"{experiment_kind} report has no {field}")


def _validate_experiment(
    *,
    database_url: str,
    experiment_id: str,
    experiment_kind: str,
    expected_preparations: int,
    expected_trials: int,
) -> None:
    with psycopg.connect(database_url) as conn:
        experiment = conn.execute(
            "SELECT experiment_kind, status FROM benchmark_experiments WHERE id = %s",
            (experiment_id,),
        ).fetchone()
        preparations = conn.execute(
            """
                SELECT count(*), count(*) FILTER (WHERE status = 'completed')
                FROM benchmark_variant_preparations WHERE experiment_id = %s
            """,
            (experiment_id,),
        ).fetchone()
        trials = conn.execute(
            """
                SELECT count(*),
                    count(*) FILTER (WHERE status = 'completed'),
                    count(*) FILTER (WHERE recovered IS TRUE),
                    count(*) FILTER (WHERE failure_code IS NOT NULL),
                    count(*) FILTER (
                        WHERE status IN ('invalid', 'infrastructure_failed')
                    )
                FROM benchmark_trials WHERE experiment_id = %s
            """,
            (experiment_id,),
        ).fetchone()
    expected = (expected_trials, expected_trials, expected_trials, 0, 0)
    if (
        experiment != (experiment_kind, "completed")
        or preparations != (expected_preparations, expected_preparations)
        or trials != expected
    ):
        raise RuntimeError(f"{experiment_kind} did not complete every required trial")


def _require_preregistration(report: dict[str, Any], *, pilot_id: str) -> None:
    if (
        str(report.get("pilot_experiment_id") or "") != pilot_id
        or not str(report.get("preregistration_sha256") or "").strip()
        or len(report.get("eligible_held_out_variant_ids") or []) != 12
        or len(report.get("selected_held_out_variant_ids") or []) != 12
        or report.get("repetitions_per_variant") != 2
    ):
        raise RuntimeError("confirmation preregistration is incomplete")


def _require_confirmation_gates(report: dict[str, Any]) -> None:
    _require_report_identity(report, experiment_kind="confirmation")
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
        *(
            f"| {name} | {'pass' if value is True else 'fail'} |"
            for name, value in sorted(gates.items())
        ),
    ]
    with path.open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
