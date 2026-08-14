"""Run shared local and hosted live-acceptance stages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
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

from hindsight.server_tenants import ACCEPTANCE_TENANT_ID, PUBLIC_DEMO_TENANT_ID

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCAL_DATABASE_HOSTS = {"localhost", "127.0.0.1", "::1"}
SEMANTIC_MAX_DISTANCE = 0.35
ACCEPTANCE_ARTIFACT_DIR_ENV = "HINDSIGHT_ACCEPTANCE_ARTIFACT_DIR"
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
    "test_live_gemini_consolidation_requires_review_before_retrieval"
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
GOVERNED_REMEDIATION_BROWSER_CONTRACT_SELECTOR = (
    "tests/test_browser_ui.py::test_operator_can_approve_model_selected_governed_memory_retraction"
)
CAUSAL_EVIDENCE_BROWSER_STATES_SELECTOR = (
    "tests/test_browser_ui.py::test_causal_evidence_states_render_fail_closed_in_browser"
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
HOSTED_ONLY_BROWSER_CONTRACT_SELECTORS = (
    GOVERNED_REMEDIATION_BROWSER_CONTRACT_SELECTOR,
    *HOSTED_ONLY_INFRASTRUCTURE_SELECTORS_BY_PHASE["browser"],
)
LOCAL_BROWSER_PRODUCT_SELECTORS = (
    "tests/test_api.py",
    "tests/test_snapshots.py",
    "tests/test_queueing.py",
    CAUSAL_EVIDENCE_BROWSER_STATES_SELECTOR,
    *SHARED_BROWSER_CONTRACT_SELECTORS,
)
HOSTED_BROWSER_PRODUCT_SELECTORS = (
    GOVERNED_REMEDIATION_BROWSER_CONTRACT_SELECTOR,
    *SHARED_BROWSER_CONTRACT_SELECTORS,
    *HOSTED_ONLY_INFRASTRUCTURE_SELECTORS_BY_PHASE["browser"],
)
WORKER_PRODUCT_SELECTORS = (
    "tests/test_hosted_acceptance.py::"
    "test_scheduled_dispatch_reclaims_and_source_terminalizes_with_quarantine",
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
HOSTED_ACCEPTANCE_MODES = ("full", "browser-only")
EXACT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SQS_QUEUE_HOST_PATTERN = re.compile(r"sqs\.([a-z0-9-]+)\.amazonaws\.com(\.cn)?")
SQS_QUEUE_ARN_PATTERN = re.compile(r"arn:(aws|aws-us-gov|aws-cn):sqs:([a-z0-9-]+):([0-9]{12}):(.+)")
AWS_RESOURCE_NAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]{3,255}")
SQS_QUEUE_NAME_PATTERN = re.compile(r"(?=.{1,80}\Z)[A-Za-z0-9_-]+(?:\.fifo)?")


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

    hosted_plan = subparsers.add_parser("hosted-plan")
    hosted_plan.add_argument("--mode", choices=HOSTED_ACCEPTANCE_MODES, required=True)
    hosted_plan.add_argument("--requested-sha", required=True)
    hosted_plan.add_argument("--candidate-ui-url", required=True)
    hosted_plan.add_argument("--github-output", type=pathlib.Path, required=True)

    args = parser.parse_args()
    if args.command == "local-product-full":
        _run_local_product_full(args)
    elif args.command == "hosted-product":
        _run_hosted_product(args)
    elif args.command == "hosted-plan":
        _plan_hosted_acceptance(args)


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


def _verify_semantic(args: argparse.Namespace) -> None:
    database_url = _required_database_url(args.database_url)
    if args.database_scope == "local":
        _validate_local_url(database_url)
        selectors = LOCAL_SEMANTIC_SELECTORS
    else:
        _require_hosted_database(database_url)
        selectors = SEMANTIC_RETRIEVAL_SELECTORS
    _require_gemini_credentials()
    env = _product_environment(database_url, tenant_id=ACCEPTANCE_TENANT_ID)
    env["RUN_LIVE_GEMINI_ACCEPTANCE"] = "1"
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
    env = _product_environment(database_url, tenant_id=ACCEPTANCE_TENANT_ID)
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
    semantic_env = _product_environment(
        semantic_url,
        tenant_id=ACCEPTANCE_TENANT_ID,
    )
    _initialize_product_database(semantic_env, configure_embeddings=True)
    _verify_semantic(argparse.Namespace(database_url=semantic_url, database_scope="local"))

    resilience_env = _product_environment(
        resilience_url,
        tenant_id=ACCEPTANCE_TENANT_ID,
    )
    _initialize_product_database(resilience_env, configure_embeddings=False)
    _verify_resilience(argparse.Namespace(database_url=resilience_url))

    browser_env = _product_environment(
        browser_url,
        tenant_id=PUBLIC_DEMO_TENANT_ID,
    )
    _initialize_product_database(browser_env, configure_embeddings=True)
    _run_local_browser_product(
        database_url=browser_url,
        base_url=args.base_url,
    )


def _initialize_product_database(env: dict[str, str], *, configure_embeddings: bool) -> None:
    _run([sys.executable, "scripts/migrate.py"], env=env)
    _run([sys.executable, "scripts/initialize_agent_storage.py"], env=env)
    if configure_embeddings:
        _run(
            [
                sys.executable,
                "scripts/reembed_memories.py",
                "--max-distance",
                str(SEMANTIC_MAX_DISTANCE),
            ],
            env=env,
        )


def _run_local_browser_product(*, database_url: str, base_url: str) -> None:
    base_url = str(base_url).rstrip("/")
    parts = urlsplit(base_url)
    if parts.scheme != "http" or parts.hostname not in LOCAL_DATABASE_HOSTS or parts.port is None:
        raise ValueError("local-product-full requires an explicit loopback HTTP port")
    _require_gemini_credentials()
    issuer = f"{base_url}/local-user-pool"
    client_id = "local-browser-client"
    _configure_local_product_identity(database_url=database_url, issuer=issuer)
    env = _product_environment(
        database_url,
        tenant_id=PUBLIC_DEMO_TENANT_ID,
    )
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
            "HINDSIGHT_COGNITO_ISSUER": issuer,
            "HINDSIGHT_COGNITO_CLIENT_ID": client_id,
            "HINDSIGHT_LOCAL_PRODUCT_ORIGIN": base_url,
            "HINDSIGHT_INLINE_WORKER": "1",
            "HINDSIGHT_RUN_DLQ_ARN": "local:sqs:hindsight-run-dlq",
            "HINDSIGHT_ALLOWED_ORIGINS": base_url,
            "HINDSIGHT_BROWSER_BASE_URL": base_url,
            "HINDSIGHT_LOCAL_AUTH_AUTO": "1",
        }
    )
    artifact_dir = _acceptance_artifact_dir("local-browser")
    env[ACCEPTANCE_ARTIFACT_DIR_ENV] = str(artifact_dir)
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tests.local_product_app:app",
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


def _configure_local_product_identity(*, database_url: str, issuer: str) -> None:
    from tests.local_product_app import LOCAL_SUBJECT

    principal_hash = hashlib.sha256(f"{issuer}\0{LOCAL_SUBJECT}".encode()).hexdigest()
    provisioning_key = hashlib.sha256(f"{issuer}\0managed-role\0operator".encode()).hexdigest()
    with psycopg.connect(
        database_url,
        application_name="hindsight-local-product-identity",
    ) as connection:
        connection.execute(
            "SELECT set_config('hindsight.tenant_id', %s, true)",
            (PUBLIC_DEMO_TENANT_ID,),
        )
        tenant = connection.execute(
            "SELECT status FROM tenants WHERE id = %s FOR UPDATE",
            (PUBLIC_DEMO_TENANT_ID,),
        ).fetchone()
        if tenant != ("active",):
            raise RuntimeError("local product identity tenant is not active")
        connection.execute(
            """
                INSERT INTO product_principal_roles (
                    principal_hash,
                    provisioning_key,
                    tenant_id,
                    role,
                    status
                )
                VALUES (%s, %s, %s, 'operator', 'active')
                ON CONFLICT (provisioning_key) DO UPDATE
                SET principal_hash = excluded.principal_hash,
                    tenant_id = excluded.tenant_id,
                    role = 'operator',
                    status = 'active',
                    updated_at = now()
            """,
            (principal_hash, provisioning_key, PUBLIC_DEMO_TENANT_ID),
        )


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
    tenant_id = _hosted_phase_tenant_id(args.phase)
    env["PGOPTIONS"] = f"-c hindsight.tenant_id={tenant_id}"
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
        _required_env("HINDSIGHT_PRODUCT_ACCESS_TOKEN")
        _verify_changefeed(env)
    elif args.phase == "worker":
        for name in (
            "HINDSIGHT_ACCEPTANCE_RUN_ATTEMPT_LEASE_SECONDS",
            "HINDSIGHT_ACCEPTANCE_QUEUE_VISIBILITY_SECONDS",
            "HINDSIGHT_ACCEPTANCE_RUN_MAX_ATTEMPTS",
            "HINDSIGHT_ACCEPTANCE_SCHEDULER_SECONDS",
        ):
            _required_positive_int_env(name)
        _required_matching_sqs_queue_env()
        _required_aws_resource_name_env("HINDSIGHT_QUARANTINE_TABLE")
        _required_aws_resource_name_env("HINDSIGHT_QUARANTINE_INDEX")
    elif args.phase == "browser":
        _verify_hosted_endpoints()
        _required_env("HINDSIGHT_OPERATOR_USERNAME")
        _required_env("HINDSIGHT_OPERATOR_PASSWORD")
        _required_env("HINDSIGHT_CHANGEFEED_AUTH_TOKEN")
        _verify_changefeed(env)
    _run_hosted_pytest(selectors, env=env, phase=args.phase)


def _plan_hosted_acceptance(args: argparse.Namespace) -> None:
    requested_sha = _require_exact_sha(args.requested_sha)
    candidate_ui_url = _require_https_url(args.candidate_ui_url, "candidate UI URL")
    outputs = {
        "acceptance_mode": args.mode,
        "run_product_preflight": "true" if args.mode == "full" else "false",
        "run_deploy": "true",
        "reuse_candidate": "false",
        "candidate_ui_url": "",
        "candidate_api_url": "",
        "candidate_websocket_url": "",
        "observed_revision": "not-checked",
    }
    if args.mode == "browser-only":
        candidate = _probe_deployed_candidate(
            expected_sha=requested_sha,
            ui_url=candidate_ui_url,
        )
        outputs["observed_revision"] = candidate["observed_revision"]
        if candidate["reusable"]:
            outputs.update(
                {
                    "run_deploy": "false",
                    "reuse_candidate": "true",
                    "candidate_ui_url": candidate["ui_url"],
                    "candidate_api_url": candidate["api_url"],
                    "candidate_websocket_url": candidate["websocket_url"],
                }
            )
    _write_github_outputs(args.github_output, outputs)


def _probe_deployed_candidate(*, expected_sha: str, ui_url: str) -> dict[str, Any]:
    try:
        health = _read_json_url(f"{ui_url}/v1/health/live")
        observed_revision = health.get("revision")
        if observed_revision != expected_sha:
            return {
                "reusable": False,
                "observed_revision": _reported_revision(observed_revision),
            }
        config = _read_runtime_config(f"{ui_url}/config.js")
        websocket_url = config.get("websocketUrl")
        if config.get("publicApiBase") != "/v1":
            raise ValueError("candidate public API base is not /v1")
        if config.get("productApiBase") != "/v2":
            raise ValueError("candidate product API base is not /v2")
        auth = config.get("auth")
        if not isinstance(auth, dict) or not isinstance(auth.get("clientId"), str):
            raise ValueError("candidate product identity configuration is unavailable")
        if not isinstance(websocket_url, str) or not websocket_url.startswith("wss://"):
            raise ValueError("candidate WebSocket endpoint must use WSS")
    except (OSError, ValueError):
        return {"reusable": False, "observed_revision": "unavailable"}
    return {
        "reusable": True,
        "observed_revision": expected_sha,
        "ui_url": ui_url,
        "api_url": ui_url,
        "websocket_url": websocket_url,
    }


def _read_runtime_config(url: str) -> dict[str, Any]:
    text = _read_text_url(url).strip()
    prefix = "window.HINDSIGHT_CONFIG = "
    if not text.startswith(prefix) or not text.endswith(";"):
        raise ValueError("candidate runtime configuration has an unexpected format")
    payload = json.loads(text[len(prefix) : -1])
    if not isinstance(payload, dict):
        raise ValueError("candidate runtime configuration must be an object")
    return payload


def _reported_revision(value: Any) -> str:
    if isinstance(value, str) and EXACT_SHA_PATTERN.fullmatch(value):
        return value
    return "invalid"


def _write_github_outputs(path: pathlib.Path, outputs: dict[str, str]) -> None:
    lines = []
    for name, value in outputs.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"GitHub output {name} contains a newline")
        lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _hosted_phase_tenant_id(phase: str) -> str:
    if phase in {"consolidation", "worker", "browser"}:
        return PUBLIC_DEMO_TENANT_ID
    return ACCEPTANCE_TENANT_ID


def _verify_changefeed(env: dict[str, str]) -> None:
    _run([sys.executable, "scripts/configure_changefeed.py", "status"], env=env)


def _run_hosted_pytest(selectors: tuple[str, ...], *, env: dict[str, str], phase: str) -> None:
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
    expected_revision = _require_exact_sha(_required_env("HINDSIGHT_EXPECTED_DEPLOYED_REVISION"))
    websocket_url = _required_env("HINDSIGHT_WEBSOCKET_URL")
    if not websocket_url.startswith("wss://"):
        raise ValueError("hosted product WebSocket endpoint must use WSS")
    for base_url in (ui_url, api_url):
        health = _read_json_url(f"{base_url}/v1/health/live")
        if health.get("revision") != expected_revision:
            raise RuntimeError("hosted product revision does not match the requested SHA")
    for url in (f"{ui_url}/v1/health/ready", f"{api_url}/v1/health/ready"):
        with request.urlopen(url, timeout=30) as response:  # noqa: S310 - guarded HTTPS URL
            if response.status != 200:
                raise RuntimeError(f"hosted product endpoint is not ready: {url}")


def _read_json_url(url: str) -> dict[str, Any]:
    payload = json.loads(_read_text_url(url))
    if not isinstance(payload, dict):
        raise ValueError("hosted endpoint response must be an object")
    return payload


def _read_text_url(url: str) -> str:
    with request.urlopen(url, timeout=30) as response:  # noqa: S310 - guarded HTTPS URL
        if response.status != 200:
            raise OSError(f"hosted endpoint returned HTTP {response.status}")
        return response.read().decode("utf-8")


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
        raise ValueError("local acceptance requires a named loopback database with sslmode=disable")
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


def _require_gemini_credentials() -> None:
    if not any(
        (os.environ.get(name) or "").strip() for name in ("GEMINI_API_KEYS", "GEMINI_API_KEY")
    ):
        raise ValueError("Gemini credentials must already be loaded into the environment")


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _required_matching_sqs_queue_env() -> tuple[str, str]:
    queue_url = _required_env("HINDSIGHT_ACCEPTANCE_RUN_QUEUE_URL")
    queue_arn = _required_env("HINDSIGHT_ACCEPTANCE_RUN_QUEUE_ARN")
    parts = urlsplit(queue_url)
    host_match = SQS_QUEUE_HOST_PATTERN.fullmatch(parts.hostname or "")
    path_parts = parts.path.split("/")
    if (
        parts.scheme != "https"
        or host_match is None
        or parts.netloc != parts.hostname
        or parts.query
        or parts.fragment
        or len(path_parts) != 3
        or path_parts[0]
        or re.fullmatch(r"[0-9]{12}", path_parts[1]) is None
        or SQS_QUEUE_NAME_PATTERN.fullmatch(path_parts[2]) is None
    ):
        raise ValueError("HINDSIGHT_ACCEPTANCE_RUN_QUEUE_URL is not an exact SQS queue URL")
    arn_match = SQS_QUEUE_ARN_PATTERN.fullmatch(queue_arn)
    if arn_match is None or SQS_QUEUE_NAME_PATTERN.fullmatch(arn_match[4]) is None:
        raise ValueError("HINDSIGHT_ACCEPTANCE_RUN_QUEUE_ARN is not an exact SQS queue ARN")
    partition, arn_region, arn_account, arn_name = arn_match.groups()
    url_region, china_endpoint = host_match.groups()
    expected_partition = (
        "aws-cn" if china_endpoint else "aws-us-gov" if url_region.startswith("us-gov-") else "aws"
    )
    if (url_region, path_parts[1], path_parts[2]) != (
        arn_region,
        arn_account,
        arn_name,
    ) or partition != expected_partition:
        raise ValueError("hosted run queue URL and ARN do not identify the same queue")
    return queue_url, queue_arn


def _required_aws_resource_name_env(name: str) -> str:
    value = _required_env(name)
    if AWS_RESOURCE_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} is not a valid AWS resource name")
    return value


def _required_https_env(name: str) -> str:
    return _require_https_url(_required_env(name), name)


def _require_https_url(value: str, label: str) -> str:
    value = value.strip().rstrip("/")
    if not value.startswith("https://"):
        raise ValueError(f"{label} must use HTTPS")
    return value


def _require_exact_sha(value: str) -> str:
    value = value.strip()
    if not EXACT_SHA_PATTERN.fullmatch(value):
        raise ValueError("requested revision must be a full lowercase hexadecimal SHA")
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


def _product_environment(database_url: str, *, tenant_id: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": database_url,
            "EMBEDDING_PROVIDER": "gemini",
            "LLM_PROVIDER": "gemini",
            "PGOPTIONS": f"-c hindsight.tenant_id={tenant_id}",
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


if __name__ == "__main__":
    main()
