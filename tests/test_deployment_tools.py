"""Deployment prerequisite tooling."""

import importlib.util
import os
import pathlib
import subprocess
import sys

import pytest
import yaml


def _configure_module():
    path = pathlib.Path("scripts/configure_demo_secrets.py")
    spec = importlib.util.spec_from_file_location("configure_demo_secrets", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _changefeed_module():
    path = pathlib.Path("scripts/configure_changefeed.py")
    spec = importlib.util.spec_from_file_location("configure_changefeed", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _deployment_identity_module():
    path = pathlib.Path("scripts/deployment_identity_preflight.py")
    spec = importlib.util.spec_from_file_location("deployment_identity_preflight", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_authorization_script(
    workflow_path: str,
    *,
    values: dict[str, str],
    output_path: pathlib.Path,
) -> subprocess.CompletedProcess[str]:
    workflow = yaml.safe_load(pathlib.Path(workflow_path).read_text())
    script = next(
        step["run"]
        for step in workflow["jobs"]["authorize"]["steps"]
        if step.get("id") in {"authorization", "authorize"}
    )
    env = os.environ.copy()
    env.update(values)
    env["GITHUB_OUTPUT"] = str(output_path)
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


def test_configure_demo_secrets_dry_run_never_prints_values(monkeypatch, capsys):
    configure = _configure_module()

    class FakeSsm:
        def get_parameter(self, **kwargs):
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "ParameterNotFound", "Message": "missing"}},
                "GetParameter",
            )

    class FakeSession:
        def client(self, *args, **kwargs):
            return FakeSsm()

    monkeypatch.setattr(configure.boto3, "Session", lambda **kwargs: FakeSession())
    monkeypatch.setattr(configure, "load_dotenv", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret-database")
    monkeypatch.setenv("HINDSIGHT_CHANGEFEED_AUTH_TOKEN", "secret-changefeed")
    for index in range(5):
        name = "GEMINI_API_KEY" if index == 0 else f"GEMINI_API_KEY_{index}"
        monkeypatch.setenv(name, f"secret-gemini-{index}")
    monkeypatch.setattr(sys, "argv", ["configure_demo_secrets.py", "--dry-run"])

    configure.main()

    output = capsys.readouterr().out
    assert "/hindsight/demo/gemini-api-keys" in output
    assert "secret-" not in output


def test_configure_demo_secrets_preserves_generated_changefeed_token_on_overwrite(
    monkeypatch, capsys
):
    configure = _configure_module()

    writes = []

    class FakeSsm:
        def get_parameter(self, **kwargs):
            return {"Parameter": {"Name": kwargs["Name"]}}

        def put_parameter(self, **kwargs):
            writes.append(kwargs)

    class FakeSession:
        def client(self, *args, **kwargs):
            return FakeSsm()

    monkeypatch.setattr(configure.boto3, "Session", lambda **kwargs: FakeSession())
    monkeypatch.setattr(configure, "load_dotenv", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://database")
    monkeypatch.delenv("HINDSIGHT_CHANGEFEED_AUTH_TOKEN", raising=False)
    for index in range(5):
        name = "GEMINI_API_KEY" if index == 0 else f"GEMINI_API_KEY_{index}"
        monkeypatch.setenv(name, f"key-{index}")
    monkeypatch.setattr(sys, "argv", ["configure_demo_secrets.py", "--overwrite"])

    configure.main()

    assert {write["Name"] for write in writes} == {
        "/hindsight/demo/database-url",
        "/hindsight/demo/gemini-api-keys",
    }
    assert "changefeed-token" in capsys.readouterr().out


def test_changefeed_wait_observes_requested_database_state(monkeypatch):
    configure = _changefeed_module()
    statuses = iter(
        [
            {"job_id": "42", "status": "pause-requested"},
            {"job_id": "42", "status": "paused"},
        ]
    )
    monkeypatch.setattr(configure, "changefeed_status", lambda **_kwargs: next(statuses))
    monkeypatch.setattr(configure.time, "sleep", lambda _seconds: None)

    result = configure._wait_for_job_status(  # noqa: SLF001 - CLI state-machine test
        job_id="42",
        expected="paused",
        changed=True,
        db_url="postgresql://db",
    )

    assert result == {"job_id": "42", "status": "paused", "changed": True}


def test_changefeed_wait_rejects_terminal_failure(monkeypatch):
    configure = _changefeed_module()
    monkeypatch.setattr(
        configure,
        "changefeed_status",
        lambda **_kwargs: {"job_id": "42", "status": "failed"},
    )

    with pytest.raises(RuntimeError, match="terminal status failed"):
        configure._wait_for_job_status(  # noqa: SLF001 - CLI state-machine test
            job_id="42",
            expected="running",
            changed=False,
            db_url="postgresql://db",
        )


def test_changefeed_status_command_fails_when_not_running(monkeypatch):
    configure = _changefeed_module()
    monkeypatch.setattr(
        configure,
        "changefeed_status",
        lambda: {"job_id": "42", "status": "failed", "changed": False},
    )
    monkeypatch.setattr(sys, "argv", ["configure_changefeed.py", "status"])

    with pytest.raises(RuntimeError, match="not running: failed"):
        configure.main()


@pytest.mark.parametrize(
    ("operation", "attempt_name", "expected"),
    [
        (
            lambda configure: configure.apply_changefeed(
                webhook_url="https://example.com/changefeed",
                auth_token="token",
                db_url="postgresql://fixture",
            ),
            "_apply_changefeed_once",
            {"job_id": "42", "status": "running", "changed": False},
        ),
        (
            lambda configure: configure.pause_changefeed(db_url="postgresql://fixture"),
            "_pause_changefeed_once",
            {"job_id": "42", "status": "paused", "changed": False},
        ),
        (
            lambda configure: configure.changefeed_status(db_url="postgresql://fixture"),
            "_changefeed_status_once",
            {"job_id": "42", "status": "running", "changed": False},
        ),
    ],
)
def test_changefeed_operations_retry_serialization_failure(
    monkeypatch, operation, attempt_name, expected
):
    configure = _changefeed_module()
    from psycopg.errors import SerializationFailure

    attempts = 0

    def flaky_attempt(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SerializationFailure("restart transaction")
        if attempt_name == "_changefeed_status_once":
            return expected
        return "42", False

    monkeypatch.setattr(configure, attempt_name, flaky_attempt)
    monkeypatch.setattr(
        configure,
        "_wait_for_job_status",
        lambda **kwargs: {
            "job_id": kwargs["job_id"],
            "status": kwargs["expected"],
            "changed": kwargs["changed"],
        },
    )

    assert operation(configure) == expected
    assert attempts == 2


def test_changefeed_serialization_retries_open_fresh_connections(monkeypatch):
    configure = _changefeed_module()
    from psycopg.errors import SerializationFailure

    connections = []

    class FakeConnection:
        def __enter__(self):
            connections.append(self)
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(configure, "connect", lambda *_args, **_kwargs: FakeConnection())

    def flaky_app_meta(_conn, _key):
        if len(connections) < configure.CHANGEFEED_TRANSACTION_ATTEMPTS:
            raise SerializationFailure("restart transaction")
        return None

    monkeypatch.setattr(configure, "_app_meta", flaky_app_meta)

    assert configure.changefeed_status(db_url="postgresql://fixture") == {
        "job_id": None,
        "status": "absent",
        "changed": False,
    }
    assert len(connections) == configure.CHANGEFEED_TRANSACTION_ATTEMPTS
    assert len({id(connection) for connection in connections}) == len(connections)


def test_changefeed_serialization_retries_are_bounded(monkeypatch):
    configure = _changefeed_module()
    from psycopg.errors import SerializationFailure

    attempts = 0

    def always_serialized(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise SerializationFailure("restart transaction")

    monkeypatch.setattr(configure, "_changefeed_status_once", always_serialized)

    with pytest.raises(SerializationFailure):
        configure.changefeed_status()
    assert attempts == configure.CHANGEFEED_TRANSACTION_ATTEMPTS


def test_changefeed_does_not_retry_non_serialization_failure(monkeypatch):
    configure = _changefeed_module()
    attempts = 0

    def non_retryable(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("configuration failed")

    monkeypatch.setattr(configure, "_pause_changefeed_once", non_retryable)

    with pytest.raises(RuntimeError, match="configuration failed"):
        configure.pause_changefeed()
    assert attempts == 1


def test_live_acceptance_is_product_only_and_never_fences_changefeed():
    workflow = pathlib.Path(".github/workflows/live-acceptance.yml").read_text()
    assert (
        "HINDSIGHT_GEMINI_REPRESENTATION: "
        "${{ vars.HINDSIGHT_GEMINI_REPRESENTATION || 'raw_control' }}"
    ) in workflow
    assert "github.triggering_actor" in workflow
    assert '"$TRIGGERING_ACTOR" == "$REPOSITORY_OWNER"' in workflow
    assert "run_live_acceptance.py hosted-product --phase providers" in workflow
    assert "run_live_acceptance.py hosted-product --phase semantic" in workflow
    for phase in ("semantic", "consolidation", "worker", "browser", "roles"):
        assert f"run_live_acceptance.py hosted-product --phase {phase}" in workflow
    shared_cli = pathlib.Path("scripts/run_live_acceptance.py").read_text()
    assert '"scripts/configure_changefeed.py", "status"' in shared_cli
    for forbidden in (
        "run_learning_benchmark.py",
        "hosted-benchmark",
        "learning-full",
        "configure_changefeed.py pause",
        "finalize-interrupted",
        "benchmark:",
        "confirmation",
    ):
        assert forbidden not in workflow


def test_live_acceptance_exercises_the_hosted_websocket_subscription_lifecycle():
    workflow = pathlib.Path(".github/workflows/live-acceptance.yml").read_text()
    browser_job = workflow.split("  browser_product:\n", 1)[1].split("  database_roles:\n", 1)[0]
    roles_job = workflow.split("  database_roles:\n", 1)[1].split(
        "  product_acceptance_complete:\n", 1
    )[0]

    assert "HINDSIGHT_WEBSOCKET_URL:" in browser_job
    assert "HINDSIGHT_STAGE: ${{ vars.HINDSIGHT_STAGE || 'demo' }}" in browser_job
    assert "needs.acceptance_plan.outputs.candidate_websocket_url" in browser_job
    assert "needs.deploy.outputs.websocket_url" in browser_job
    assert "HINDSIGHT_EXPECTED_DEPLOYED_REVISION" in browser_job
    assert 'CHANGEFEED_TOKEN="$(aws ssm get-parameter --name "$CHANGEFEED_PARAMETER"' in browser_job
    assert "aws lambda get-function-configuration" in browser_job
    assert "HINDSIGHT_CHANGEFEED_IDEMPOTENCY_TABLE" in browser_job
    assert (
        'for value in "$DATABASE_URL" "$GEMINI_MATERIAL" "$HINDSIGHT_OPERATOR_USERNAME" '
        '"$HINDSIGHT_OPERATOR_PASSWORD" "$CHANGEFEED_TOKEN"'
        in browser_job
    )
    assert "HINDSIGHT_BROWSER_OPERATOR_TOKEN" not in browser_job
    assert 'echo "::add-mask::$value"' in browser_job
    assert "HINDSIGHT_CHANGEFEED_AUTH_TOKEN" in browser_job
    assert "HINDSIGHT_SELENIUM_REMOTE_URL: http://127.0.0.1:4444" in browser_job
    assert "scripts/publish_controlled_incident_telemetry.py" in browser_job
    assert "--confirm-controlled-fixture" in browser_job
    assert "--visibility-timeout-seconds 120" in browser_job
    assert "selenium/standalone-firefox@sha256:" in browser_job
    assert "--shm-size=2g" in browser_job
    assert 'docker ps -aq --filter "name=^/${container_name}$"' in browser_job
    assert 'docker logs "$container_name"' in browser_job
    assert 'docker rm -f "$container_name"' in browser_job
    assert "run_live_acceptance.py hosted-product --phase browser" in browser_job
    for name, default in (
        ("HINDSIGHT_DEPLOY_DATABASE_URL_PARAM", "/hindsight/demo/database-url"),
        ("HINDSIGHT_API_DATABASE_URL_PARAM", "/hindsight/demo/api-database-url"),
        ("HINDSIGHT_WORKER_DATABASE_URL_PARAM", "/hindsight/demo/worker-database-url"),
    ):
        assert f"{name}: ${{{{ env." not in roles_job
        assert f"{name}: ${{{{ vars.{name} || '{default}' }}}}" in roles_job


def test_hosted_environment_mutations_share_one_outer_concurrency_lock():
    live = pathlib.Path(".github/workflows/live-acceptance.yml").read_text()
    deploy = pathlib.Path(".github/workflows/deploy-demo.yml").read_text()
    destroy = pathlib.Path(".github/workflows/destroy-demo.yml").read_text()

    live_concurrency = live.split("\nconcurrency:\n", 1)[1].split("\nenv:\n", 1)[0]
    deploy_concurrency = deploy.split("\nconcurrency:\n", 1)[1].split("\nenv:\n", 1)[0]
    destroy_concurrency = destroy.split("\nconcurrency:\n", 1)[1].split("\nenv:\n", 1)[0]

    assert (
        "group: hindsight-${{ inputs.deployment_environment || 'demo' }}-environment-v2"
        in live_concurrency
    )
    assert (
        "group: hindsight-${{ inputs.deployment_environment || 'demo' }}-environment-v2"
        in destroy_concurrency
    )
    assert "inputs.validation_mode" in deploy_concurrency
    assert "format('hindsight-live-deploy-{0}-{1}'" in deploy_concurrency
    assert "format('hindsight-{0}-environment-v2'" in deploy_concurrency
    for concurrency in (
        live_concurrency,
        deploy_concurrency,
        destroy_concurrency,
    ):
        assert "cancel-in-progress: false" in concurrency


def test_live_acceptance_resolves_one_owner_authorized_revision():
    workflow = pathlib.Path(".github/workflows/live-acceptance.yml").read_text()
    parsed = yaml.safe_load(workflow)
    authorize = workflow.split("  authorize:\n", 1)[1].split("  exact_main_ci:\n", 1)[0]

    assert "  workflow_dispatch:\n" in workflow
    assert "  schedule:\n" in workflow
    assert "pull_request:" not in workflow
    assert '"$REF_NAME" == "refs/heads/main"' in authorize
    assert '"$WORKFLOW_REF" == ' in authorize
    assert '"$ACTOR" == "$REPOSITORY_OWNER"' in authorize
    assert '"$TRIGGERING_ACTOR" == "$REPOSITORY_OWNER"' in authorize
    assert "product_sha: ${{ steps.authorization.outputs.product_sha }}" in authorize
    assert 'echo "product_sha=$EVENT_SHA"' in authorize
    assert "source_sha: ${{ needs.authorize.outputs.product_sha }}" in workflow
    assert set(parsed["jobs"]) == {
        "authorize",
        "exact_main_ci",
        "acceptance_plan",
        "product_preflight",
        "deploy",
        "semantic_product",
        "consolidation_product",
        "worker_product",
        "browser_product",
        "database_roles",
        "product_acceptance_complete",
    }
    assert parsed["jobs"]["exact_main_ci"]["needs"] == "authorize"
    assert parsed["jobs"]["exact_main_ci"]["permissions"] == {
        "actions": "read",
        "contents": "read",
    }
    assert parsed["jobs"]["acceptance_plan"]["needs"] == [
        "authorize",
        "exact_main_ci",
    ]
    assert parsed["jobs"]["product_preflight"]["needs"] == [
        "authorize",
        "exact_main_ci",
        "acceptance_plan",
    ]
    assert parsed["jobs"]["deploy"]["needs"] == [
        "authorize",
        "exact_main_ci",
        "acceptance_plan",
        "product_preflight",
    ]
    for job_name in (
        "semantic_product",
        "consolidation_product",
        "worker_product",
        "database_roles",
    ):
        assert parsed["jobs"][job_name]["needs"] == ["authorize", "deploy"]
    assert parsed["jobs"]["browser_product"]["needs"] == [
        "authorize",
        "acceptance_plan",
        "deploy",
    ]
    assert parsed["jobs"]["product_acceptance_complete"]["needs"] == [
        "authorize",
        "exact_main_ci",
        "product_preflight",
        "deploy",
        "semantic_product",
        "consolidation_product",
        "worker_product",
        "browser_product",
        "database_roles",
    ]


def test_live_acceptance_modes_preserve_full_gate_and_isolate_browser_diagnostics():
    workflow = pathlib.Path(".github/workflows/live-acceptance.yml").read_text()
    parsed = yaml.safe_load(workflow)
    inputs = parsed[True]["workflow_dispatch"]["inputs"]
    jobs = parsed["jobs"]

    assert inputs["acceptance_mode"] == {
        "description": "Authoritative full gate or browser-only diagnostic",
        "required": True,
        "type": "choice",
        "default": "full",
        "options": ["full", "browser-only"],
    }
    assert "hosted-plan" in next(
        step["run"] for step in jobs["acceptance_plan"]["steps"] if step.get("id") == "plan"
    )
    assert jobs["product_preflight"]["if"] == (
        "needs.acceptance_plan.outputs.run_product_preflight == 'true'"
    )
    for job_name in (
        "semantic_product",
        "consolidation_product",
        "worker_product",
        "database_roles",
    ):
        assert jobs[job_name]["if"] == ("needs.authorize.outputs.acceptance_mode == 'full'")
    assert jobs["worker_product"]["needs"] == ["authorize", "deploy"]
    assert "reuse_candidate" in jobs["browser_product"]["if"]
    assert jobs["product_acceptance_complete"]["if"] == (
        "always() && needs.authorize.outputs.acceptance_mode == 'full'"
    )
    assert "acceptance_mode" not in parsed["concurrency"]["group"]


def test_verify_deployed_is_owner_authorized_exact_revision_and_read_only(tmp_path):
    workflow_path = ".github/workflows/verify-deployed.yml"
    workflow = pathlib.Path(workflow_path).read_text()
    parsed = yaml.safe_load(workflow)
    authorize = workflow.split("  authorize:\n", 1)[1].split("  verify:\n", 1)[0]
    verify = workflow.split("  verify:\n", 1)[1]

    assert "  workflow_dispatch:\n" in workflow
    assert "pull_request:" not in workflow
    assert set(parsed["jobs"]) == {"authorize", "verify"}
    assert parsed["jobs"]["verify"]["needs"] == "authorize"
    assert parsed["jobs"]["verify"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }
    assert '"$REF_NAME" == "refs/heads/main"' in authorize
    assert '"$WORKFLOW_REF" == ' in authorize
    assert '"$ACTOR" == "$REPOSITORY_OWNER"' in authorize
    assert '"$TRIGGERING_ACTOR" == "$REPOSITORY_OWNER"' in authorize
    assert "=~ ^[0-9a-f]{40}$" in authorize
    assert "vars.HINDSIGHT_MONITORED_SHA" in workflow

    output = tmp_path / "authorized-output"
    result = _run_authorization_script(
        workflow_path,
        values={
            "EVENT_NAME": "workflow_dispatch",
            "REF_NAME": "refs/heads/main",
            "WORKFLOW_REF": ("owner/project/.github/workflows/verify-deployed.yml@refs/heads/main"),
            "REPOSITORY": "owner/project",
            "ACTOR": "owner",
            "TRIGGERING_ACTOR": "owner",
            "REPOSITORY_OWNER": "owner",
            "REQUESTED_SHA": "a" * 40,
        },
        output_path=output,
    )
    assert result.returncode == 0
    assert f"expected_sha={'a' * 40}" in output.read_text()
    assert "deployment_environment=demo" in output.read_text()
    rejected = _run_authorization_script(
        workflow_path,
        values={
            "EVENT_NAME": "workflow_dispatch",
            "REF_NAME": "refs/heads/main",
            "WORKFLOW_REF": ("owner/project/.github/workflows/verify-deployed.yml@refs/heads/main"),
            "REPOSITORY": "owner/project",
            "ACTOR": "owner",
            "TRIGGERING_ACTOR": "maintainer",
            "REPOSITORY_OWNER": "owner",
            "REQUESTED_SHA": "a" * 40,
        },
        output_path=tmp_path / "rejected-output",
    )
    assert rejected.returncode != 0
    scheduled_output = tmp_path / "scheduled-output"
    scheduled = _run_authorization_script(
        workflow_path,
        values={
            "EVENT_NAME": "schedule",
            "REF_NAME": "refs/heads/main",
            "WORKFLOW_REF": (
                "owner/project/.github/workflows/verify-deployed.yml@refs/heads/main"
            ),
            "REPOSITORY": "owner/project",
            "ACTOR": "schedule-owner",
            "TRIGGERING_ACTOR": "schedule-owner",
            "REPOSITORY_OWNER": "owner",
            "REQUESTED_SHA": "b" * 40,
            "REQUESTED_ENVIRONMENT": "demo",
        },
        output_path=scheduled_output,
    )
    assert scheduled.returncode == 0
    assert f"expected_sha={'b' * 40}" in scheduled_output.read_text()
    assert "deployment_environment=demo" in scheduled_output.read_text()

    assert "ref: ${{ needs.authorize.outputs.expected_sha }}" in verify
    assert "Verify exact source revision" in verify
    assert "aws apigatewayv2 get-apis" in verify
    assert "hindsight-${HINDSIGHT_STAGE}-http" in verify
    assert "hindsight-${HINDSIGHT_STAGE}-websocket" in verify
    assert 'f"{api_url}/v1/health/live"' in verify
    assert 'f"{api_url}/v1/health/ready"' in verify
    assert 'f"{api_url}/v1/incidents?limit=1"' in verify
    assert 'f"{ui_url}/v1/health/ready"' in verify
    assert 'revision": expected_sha' in verify
    assert "/v1/realtime/ticket" in verify
    assert "urlencode({'ticket': ticket})" in verify
    assert "from websockets.asyncio.client import connect" in verify
    assert 'RuntimeError("ticketed WebSocket verification failed")' in verify
    python_steps = [
        step["run"]
        for step in parsed["jobs"]["verify"]["steps"]
        if "uv run python - <<'PY'" in step.get("run", "")
    ]
    assert len(python_steps) == 2
    for run in python_steps:
        source = run.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        compile(source, workflow_path, "exec")
    for forbidden in (
        "terraform",
        "scripts/migrate.py",
        "initialize_agent_storage.py",
        "apply_database_roles.py",
        "deployment_preflight.py",
        "configure_changefeed.py",
    ):
        assert forbidden not in workflow


def test_product_preflight_does_not_repeat_normal_ci():
    workflow = pathlib.Path(".github/workflows/live-acceptance.yml").read_text()
    preflight = workflow.split("  product_preflight:\n", 1)[1].split("  deploy:\n", 1)[0]

    assert "scripts/migrate.py" in preflight
    assert "scripts/initialize_agent_storage.py" in preflight
    assert "hosted-product --phase providers" in preflight
    for duplicate in (
        "uv lock --check",
        "ruff check",
        "pytest -q",
        "build_lambda_artifacts.py",
        "smoke_lambda_artifacts.py",
        "node --input-type=module --check",
        "terraform fmt",
        "terraform validate",
        "terraform test",
    ):
        assert duplicate not in preflight


def test_product_preflight_always_removes_its_run_scoped_database():
    workflow = pathlib.Path(".github/workflows/live-acceptance.yml").read_text()
    parsed = yaml.safe_load(workflow)
    preflight = parsed["jobs"]["product_preflight"]

    assert preflight["env"]["COMPOSE_PROJECT_NAME"] == (
        "hindsight_product_preflight_${{ github.run_id }}_${{ github.run_attempt }}"
    )
    cleanup = next(
        step for step in preflight["steps"] if step.get("name") == "Remove isolated CockroachDB"
    )
    assert cleanup["if"] == "always()"
    assert cleanup["run"] == "docker compose down --volumes --remove-orphans"
    assert preflight["steps"].index(cleanup) == len(preflight["steps"]) - 1


def test_exact_main_ci_query_does_not_hide_unsuccessful_runs():
    workflow = pathlib.Path(".github/workflows/live-acceptance.yml").read_text()
    job = workflow.split("  exact_main_ci:\n", 1)[1].split("  product_preflight:\n", 1)[0]

    assert "actions: read" in job
    assert "GITHUB_API_URL: ${{ github.api_url }}" in job
    assert "GITHUB_TOKEN: ${{ github.token }}" in job
    assert "--fetch" in job
    assert "gh api" not in job
    assert "scripts/verify_ci_provenance.py" in job


def test_live_acceptance_authorization_fails_closed(tmp_path):
    repository = "owner/project"
    owner = "owner"
    main_sha = "a" * 40
    base = {
        "EVENT_NAME": "workflow_dispatch",
        "REF_NAME": "refs/heads/main",
        "EVENT_SHA": main_sha,
        "LABEL_NAME": "",
        "HEAD_SHA": "",
        "HEAD_REPOSITORY": "",
        "REPOSITORY": repository,
        "WORKFLOW_REF": (f"{repository}/.github/workflows/live-acceptance.yml@refs/heads/main"),
        "ACTOR": owner,
        "TRIGGERING_ACTOR": owner,
        "REPOSITORY_OWNER": owner,
    }

    accepted_output = tmp_path / "accepted"
    accepted = _run_authorization_script(
        ".github/workflows/live-acceptance.yml",
        values=base,
        output_path=accepted_output,
    )

    assert accepted.returncode == 0, accepted.stderr
    assert f"product_sha={main_sha}" in accepted_output.read_text()

    for index, overrides in enumerate(
        (
            {"REF_NAME": "refs/heads/feature"},
            {"TRIGGERING_ACTOR": "different-user"},
            {"WORKFLOW_REF": f"{repository}/.github/workflows/other.yml@refs/heads/main"},
            {"EVENT_SHA": "not-a-sha"},
            {"REQUESTED_MODE": "unsupported"},
        )
    ):
        rejected = _run_authorization_script(
            ".github/workflows/live-acceptance.yml",
            values={**base, **overrides},
            output_path=tmp_path / f"rejected-{index}",
        )
        assert rejected.returncode != 0

    pull_request = _run_authorization_script(
        ".github/workflows/live-acceptance.yml",
        values={
            **base,
            "EVENT_NAME": "pull_request",
            "REF_NAME": "refs/pull/42/merge",
            "LABEL_NAME": "live-acceptance",
            "HEAD_SHA": "b" * 40,
            "HEAD_REPOSITORY": repository,
            "WORKFLOW_REF": (
                f"{repository}/.github/workflows/live-acceptance.yml@refs/pull/42/merge"
            ),
        },
        output_path=tmp_path / "pull-request",
    )

    assert pull_request.returncode != 0


@pytest.mark.parametrize(
    "failed_result",
    (
        "AUTHORIZE_RESULT",
        "EXACT_MAIN_CI_RESULT",
        "PREFLIGHT_RESULT",
        "DEPLOY_RESULT",
        "SEMANTIC_RESULT",
        "CONSOLIDATION_RESULT",
        "WORKER_RESULT",
        "BROWSER_RESULT",
        "DATABASE_ROLES_RESULT",
    ),
)
def test_product_completion_requires_every_product_job(tmp_path, failed_result):
    workflow = yaml.safe_load(pathlib.Path(".github/workflows/live-acceptance.yml").read_text())
    step = workflow["jobs"]["product_acceptance_complete"]["steps"][0]
    script = step["run"]
    base = {
        "AUTHORIZED": "true",
        "AUTHORIZE_RESULT": "success",
        "EXACT_MAIN_CI_RESULT": "success",
        "PREFLIGHT_RESULT": "success",
        "DEPLOY_RESULT": "success",
        "SEMANTIC_RESULT": "success",
        "CONSOLIDATION_RESULT": "success",
        "WORKER_RESULT": "success",
        "BROWSER_RESULT": "success",
        "DATABASE_ROLES_RESULT": "success",
        "HEAD_SHA": "a" * 40,
        "UI_URL": "https://ui.example.invalid",
        "API_URL": "https://api.example.invalid",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }

    accepted = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        check=False,
        capture_output=True,
        env={**os.environ, **base},
        text=True,
    )
    rejected = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        check=False,
        capture_output=True,
        env={**os.environ, **base, failed_result: "failure"},
        text=True,
    )

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode != 0


def test_deploy_authorization_distinguishes_reusable_and_direct_dispatch(tmp_path):
    repository = "owner/project"
    owner = "owner"
    main_sha = "c" * 40
    reusable = {
        "EVENT_NAME": "workflow_dispatch",
        "REF_NAME": "refs/heads/main",
        "EVENT_SHA": main_sha,
        "PR_HEAD_SHA": "",
        "MANUAL_OPERATION": "",
        "REQUESTED_APPLY": "true",
        "REQUESTED_HEALTH_ONLY": "false",
        "REQUESTED_SOURCE_SHA": main_sha,
        "VALIDATION_MODE": "true",
        "CALLER_WORKFLOW": "live acceptance",
        "CALLER_WORKFLOW_REF": (
            f"{repository}/.github/workflows/live-acceptance.yml@refs/heads/main"
        ),
        "LABEL_NAME": "",
        "HEAD_REPOSITORY": "",
        "REPOSITORY": repository,
        "ACTOR": owner,
        "TRIGGERING_ACTOR": owner,
        "REPOSITORY_OWNER": owner,
        "EMBEDDING_MAX_DISTANCE": "0.35",
    }

    reusable_output = tmp_path / "reusable"
    accepted = _run_authorization_script(
        ".github/workflows/deploy-demo.yml",
        values=reusable,
        output_path=reusable_output,
    )

    assert accepted.returncode == 0, accepted.stderr
    assert "should_apply=true" in reusable_output.read_text()
    assert f"source_sha={main_sha}" in reusable_output.read_text()

    for index, overrides in enumerate(
        (
            {"REQUESTED_SOURCE_SHA": "d" * 40},
            {"REQUESTED_HEALTH_ONLY": "true"},
            {"CALLER_WORKFLOW_REF": f"{repository}/.github/workflows/other.yml@refs/heads/main"},
            {"EVENT_NAME": "pull_request", "REF_NAME": "refs/pull/42/merge"},
        )
    ):
        rejected = _run_authorization_script(
            ".github/workflows/deploy-demo.yml",
            values={**reusable, **overrides},
            output_path=tmp_path / f"reusable-rejected-{index}",
        )
        assert rejected.returncode != 0

    direct_output = tmp_path / "direct"
    direct = _run_authorization_script(
        ".github/workflows/deploy-demo.yml",
        values={
            **reusable,
            "MANUAL_OPERATION": "plan",
            "REQUESTED_APPLY": "false",
            "REQUESTED_HEALTH_ONLY": "true",
            "REQUESTED_SOURCE_SHA": "",
            "VALIDATION_MODE": "false",
            "CALLER_WORKFLOW": "deploy demo",
            "CALLER_WORKFLOW_REF": (
                f"{repository}/.github/workflows/deploy-demo.yml@refs/heads/main"
            ),
        },
        output_path=direct_output,
    )

    assert direct.returncode == 0, direct.stderr
    assert "should_apply=false" in direct_output.read_text()
    assert f"source_sha={main_sha}" in direct_output.read_text()
    assert "deployment_environment=demo" in direct_output.read_text()

    candidate_output = tmp_path / "candidate"
    candidate = _run_authorization_script(
        ".github/workflows/deploy-demo.yml",
        values={
            **reusable,
            "REQUESTED_ENVIRONMENT": "demo-candidate",
        },
        output_path=candidate_output,
    )
    assert candidate.returncode == 0, candidate.stderr
    assert "deployment_environment=demo-candidate" in candidate_output.read_text()

    rejected_environment = _run_authorization_script(
        ".github/workflows/deploy-demo.yml",
        values={**reusable, "REQUESTED_ENVIRONMENT": "untrusted"},
        output_path=tmp_path / "environment-rejected",
    )
    assert rejected_environment.returncode != 0
    assert "Unsupported deployment environment" in rejected_environment.stderr


def test_deployment_identity_preflight_binds_account_state_and_certificate(capsys):
    identity = _deployment_identity_module()

    class FakeSts:
        def get_caller_identity(self):
            return {"Account": "123456789012"}

    class FakeS3:
        def __init__(self):
            self.calls = []

        def get_bucket_location(self, **kwargs):
            self.calls.append(kwargs)
            return {"LocationConstraint": None}

        def get_bucket_versioning(self, **kwargs):
            self.calls.append(kwargs)
            return {"Status": "Enabled"}

    class FakeAcm:
        def describe_certificate(self, **kwargs):
            return {"Certificate": {"Status": "ISSUED"}}

    s3 = FakeS3()
    identity.verify_deployment_identity(
        expected_account_id="123456789012",
        region="us-east-1",
        state_bucket="target-state",
        certificate_arn=(
            "arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-000000000000"
        ),
        sts_client=FakeSts(),
        s3_client=s3,
        acm_client=FakeAcm(),
    )

    assert all(call["ExpectedBucketOwner"] == "123456789012" for call in s3.calls)
    assert "versioned state bucket" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("caller_account", "certificate_arn", "error"),
    (
        (
            "210987654321",
            "arn:aws:acm:us-east-1:123456789012:certificate/test",
            "unexpected account",
        ),
        (
            "123456789012",
            "arn:aws:acm:us-east-1:210987654321:certificate/test",
            "does not belong",
        ),
    ),
)
def test_deployment_identity_preflight_rejects_mixed_accounts(
    caller_account,
    certificate_arn,
    error,
):
    identity = _deployment_identity_module()

    class FakeSts:
        def get_caller_identity(self):
            return {"Account": caller_account}

    class FakeS3:
        def get_bucket_location(self, **kwargs):
            return {"LocationConstraint": None}

        def get_bucket_versioning(self, **kwargs):
            return {"Status": "Enabled"}

    class FakeAcm:
        def describe_certificate(self, **kwargs):
            return {"Certificate": {"Status": "ISSUED"}}

    with pytest.raises(RuntimeError, match=error):
        identity.verify_deployment_identity(
            expected_account_id="123456789012",
            region="us-east-1",
            state_bucket="target-state",
            certificate_arn=certificate_arn,
            sts_client=FakeSts(),
            s3_client=FakeS3(),
            acm_client=FakeAcm(),
        )


def test_deployment_identity_preflight_rejects_unisolated_candidate():
    identity = _deployment_identity_module()

    with pytest.raises(RuntimeError, match="stage and state key"):
        identity.verify_deployment_identity(
            expected_account_id="123456789012",
            region="us-east-1",
            state_bucket="target-state",
            certificate_arn="arn:aws:acm:us-east-1:123456789012:certificate/test",
            deployment_environment="demo-candidate",
            stage="demo",
            state_key="hindsight/demo/terraform.tfstate",
            domain_name="candidate.hindsight.strathmoreedu.qzz.io",
        )

    with pytest.raises(RuntimeError, match="source DNS ownership"):
        identity.verify_deployment_identity(
            expected_account_id="123456789012",
            region="us-east-1",
            state_bucket="target-state",
            certificate_arn="arn:aws:acm:us-east-1:123456789012:certificate/test",
            deployment_environment="demo-candidate",
            stage="candidate",
            state_key="hindsight/demo-candidate/terraform.tfstate",
            domain_name="hindsight.strathmoreedu.qzz.io",
        )


def test_validation_deployment_selects_bounded_runtime_timing():
    workflow = pathlib.Path(".github/workflows/deploy-demo.yml").read_text()

    assert "TF_VAR_validation_mode: ${{ inputs.validation_mode && 'true' || 'false' }}" in workflow
    for output in (
        "worker_timeout_seconds",
        "run_attempt_lease_seconds",
        "run_queue_visibility_seconds",
        "run_max_attempts",
        "run_dispatch_schedule_seconds",
    ):
        assert f"output -raw {output}" in workflow
        assert f"value: ${{{{ jobs.apply.outputs.{output} }}}}" in workflow


def test_hosted_browser_preserves_failure_evidence_without_learning_dependency():
    workflow = pathlib.Path(".github/workflows/live-acceptance.yml").read_text()
    browser = workflow.split("  browser_product:\n", 1)[1].split("  database_roles:\n", 1)[0]

    assert "HINDSIGHT_ACCEPTANCE_ARTIFACT_DIR" in browser
    job_environment = browser.split("    env:\n", 1)[1].split("    steps:\n", 1)[0]
    assert "runner.temp" not in job_environment
    assert "HINDSIGHT_ACCEPTANCE_ARTIFACT_DIR=$RUNNER_TEMP/hindsight-browser-evidence" in browser
    assert '>> "$GITHUB_ENV"' in browser
    assert "if: always()" in browser
    assert "browser-evidence-" in browser
    assert "if-no-files-found: error" in browser
    for forbidden in ("benchmark", "pilot", "preregister", "confirmation", "learning"):
        assert forbidden not in browser


def test_local_setup_enables_vector_indexing_and_disables_ssm_resolution():
    makefile = pathlib.Path("Makefile").read_text()

    dev_up = makefile.split("dev-up:\n", 1)[1].split("\ndev-down:", 1)[0]
    product_api = makefile.split("product-api-local:\n", 1)[1].split("\nchangefeed-apply:", 1)[0]
    assert "feature.vector_index.enabled = true" in dev_up
    assert 'HINDSIGHT_DATABASE_URL_PARAM=""' in product_api
    assert 'HINDSIGHT_GEMINI_API_KEY_PARAM=""' in product_api
    assert 'HINDSIGHT_GEMINI_API_KEYS_PARAM=""' in product_api
