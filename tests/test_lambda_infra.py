"""Static checks for the Terraform-owned Lambda deployment."""

import pathlib


def test_application_stack_uses_split_artifacts_and_external_secret_references():
    stack = pathlib.Path("infra/terraform/app/main.tf").read_text()

    assert 'handler       = "hindsight.api.handler"' in stack
    assert 'handler       = "hindsight.worker.handler"' in stack
    assert 'handler       = "hindsight.realtime.websocket_handler"' in stack
    assert 'handler       = "hindsight.realtime.changefeed_handler"' in stack
    assert "aws_ssm_parameter" not in stack
    assert "HINDSIGHT_DATABASE_URL_PARAM" in stack
    assert "HINDSIGHT_CHANGEFEED_AUTH_TOKEN_PARAM" in stack


def test_destroy_workflow_pauses_changefeed_and_requires_confirmation():
    workflow = pathlib.Path(".github/workflows/destroy-demo.yml").read_text()

    assert "destroy-demo" in workflow
    assert "configure_changefeed.py pause" in workflow
    assert "environment: demo" in workflow
    assert "plan -destroy" in workflow


def test_bootstrap_state_is_protected_from_destroy():
    bootstrap = pathlib.Path("infra/terraform/bootstrap/main.tf").read_text()

    assert "prevent_destroy = true" in bootstrap
    assert "aws_ssm_parameter" not in bootstrap
