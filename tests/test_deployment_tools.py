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
    monkeypatch.setenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", "secret-operator")
    monkeypatch.setenv("HINDSIGHT_CHANGEFEED_AUTH_TOKEN", "secret-changefeed")
    for index in range(5):
        name = "GEMINI_API_KEY" if index == 0 else f"GEMINI_API_KEY_{index}"
        monkeypatch.setenv(name, f"secret-gemini-{index}")
    monkeypatch.setattr(sys, "argv", ["configure_demo_secrets.py", "--dry-run"])

    configure.main()

    output = capsys.readouterr().out
    assert "/hindsight/demo/gemini-api-keys" in output
    assert "secret-" not in output


def test_configure_demo_secrets_preserves_generated_tokens_on_overwrite(monkeypatch, capsys):
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
    monkeypatch.delenv("HINDSIGHT_FUNCTION_AUTH_TOKEN", raising=False)
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
    assert "operator-token" in capsys.readouterr().out


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


def test_live_acceptance_restores_changefeed_after_benchmark_failure():
    workflow = pathlib.Path(".github/workflows/live-acceptance.yml").read_text()
    assert "github.triggering_actor" in workflow
    assert '"$TRIGGERING_ACTOR" == "$REPOSITORY_OWNER"' in workflow
    assert "test_live_gemini_embedding_provider" in workflow
    assert "BEDROCK" not in workflow
    assert "Bedrock" not in workflow
    restore_job = workflow.split("  restore_changefeed:\n", 1)[1].split(
        "  browser_acceptance:\n", 1
    )[0]

    assert "if: always() && needs.deploy.result == 'success'" in restore_job
    assert "if: needs.benchmark.result != 'success'" in restore_job
    assert "run_learning_benchmark.py finalize-interrupted" in restore_job
    restore_step = restore_job.split("- name: Restore and verify the managed changefeed", 1)[1]
    assert "if: always()" in restore_step
    assert "configure_changefeed.py apply" in restore_step
    assert "configure_changefeed.py status" in restore_step


def test_live_acceptance_exercises_the_hosted_websocket_subscription_lifecycle():
    workflow = pathlib.Path(".github/workflows/live-acceptance.yml").read_text()
    browser_job = workflow.split("  browser_acceptance:\n", 1)[1].split(
        "  acceptance_complete:\n", 1
    )[0]

    assert (
        "HINDSIGHT_WEBSOCKET_URL: ${{ needs.deploy.outputs.websocket_url }}" in browser_job
    )
    assert 'CHANGEFEED_TOKEN="$(aws ssm get-parameter --name "$CHANGEFEED_PARAMETER"' in browser_job
    assert 'echo "::add-mask::$CHANGEFEED_TOKEN"' in browser_job
    assert "HINDSIGHT_CHANGEFEED_AUTH_TOKEN" in browser_job
    assert "test_hosted_acceptance.py" in browser_job
    assert "WebSocket reconnect/resubscribe/unsubscribe" in browser_job


def test_live_acceptance_resolves_one_owner_authorized_revision():
    workflow = pathlib.Path(".github/workflows/live-acceptance.yml").read_text()
    authorize = workflow.split("  authorize:\n", 1)[1].split("  verify:\n", 1)[0]
    operational_jobs = workflow.split("  verify:\n", 1)[1]

    assert "  workflow_dispatch:\n" in workflow
    assert '"$REF_NAME" == "refs/heads/main"' in authorize
    assert '"$WORKFLOW_REF" == ' in authorize
    assert '"$ACTOR" == "$REPOSITORY_OWNER"' in authorize
    assert '"$TRIGGERING_ACTOR" == "$REPOSITORY_OWNER"' in authorize
    assert "acceptance_sha: ${{ steps.authorization.outputs.acceptance_sha }}" in authorize
    assert 'echo "acceptance_sha=$ACCEPTANCE_SHA"' in authorize
    assert "github.event.pull_request.head.sha" not in operational_jobs
    assert workflow.count("ref: ${{ needs.authorize.outputs.acceptance_sha }}") == 5
    assert workflow.count("Verify exact acceptance revision") == 5
    assert (
        workflow.count(
            "HINDSIGHT_BENCHMARK_CODE_SHA: ${{ needs.authorize.outputs.acceptance_sha }}"
        )
        == 2
    )
    assert (
        "name: live-benchmark-${{ needs.authorize.outputs.acceptance_sha }}-"
        "${{ github.run_attempt }}"
    ) in workflow
    assert "source_sha: ${{ needs.authorize.outputs.acceptance_sha }}" in workflow

    for job_name, next_job in (
        ("deploy", "semantic_acceptance"),
        ("semantic_acceptance", "benchmark"),
        ("benchmark", "restore_changefeed"),
        ("restore_changefeed", "browser_acceptance"),
        ("browser_acceptance", "acceptance_complete"),
    ):
        job = workflow.split(f"  {job_name}:\n", 1)[1].split(f"  {next_job}:\n", 1)[0]
        assert "- authorize" in job


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
    assert f"acceptance_sha={main_sha}" in accepted_output.read_text()

    for index, overrides in enumerate(
        (
            {"REF_NAME": "refs/heads/feature"},
            {"TRIGGERING_ACTOR": "different-user"},
            {"WORKFLOW_REF": f"{repository}/.github/workflows/other.yml@refs/heads/main"},
            {"EVENT_SHA": "not-a-sha"},
        )
    ):
        rejected = _run_authorization_script(
            ".github/workflows/live-acceptance.yml",
            values={**base, **overrides},
            output_path=tmp_path / f"rejected-{index}",
        )
        assert rejected.returncode != 0

    pull_ref = "refs/pull/42/merge"
    head_sha = "b" * 40
    pull_output = tmp_path / "pull-request"
    pull_request = _run_authorization_script(
        ".github/workflows/live-acceptance.yml",
        values={
            **base,
            "EVENT_NAME": "pull_request",
            "REF_NAME": pull_ref,
            "LABEL_NAME": "live-acceptance",
            "HEAD_SHA": head_sha,
            "HEAD_REPOSITORY": repository,
            "WORKFLOW_REF": (f"{repository}/.github/workflows/live-acceptance.yml@{pull_ref}"),
        },
        output_path=pull_output,
    )

    assert pull_request.returncode == 0, pull_request.stderr
    assert f"acceptance_sha={head_sha}" in pull_output.read_text()


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


def test_local_setup_enables_vector_indexing_and_disables_ssm_resolution():
    makefile = pathlib.Path("Makefile").read_text()

    dev_up = makefile.split("dev-up:\n", 1)[1].split("\ndev-down:", 1)[0]
    product_api = makefile.split("product-api-local:\n", 1)[1].split("\nchangefeed-apply:", 1)[0]
    assert "feature.vector_index.enabled = true" in dev_up
    assert 'HINDSIGHT_DATABASE_URL_PARAM=""' in product_api
    assert 'HINDSIGHT_GEMINI_API_KEY_PARAM=""' in product_api
    assert 'HINDSIGHT_GEMINI_API_KEYS_PARAM=""' in product_api


def test_telemetry_demo_uses_the_configured_local_database_url():
    makefile = pathlib.Path("Makefile").read_text()

    telemetry_demo = makefile.split("telemetry-demo:\n", 1)[1].split("\npoison-rewind-demo:", 1)[0]
    assert (
        'DATABASE_URL="$(LOCAL_DATABASE_URL)" uv run python scripts/run_telemetry_demo.py'
    ) in telemetry_demo
