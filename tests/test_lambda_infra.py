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
    assert "HINDSIGHT_GEMINI_API_KEYS_PARAM" in stack
    assert "HINDSIGHT_CHANGEFEED_AUTH_TOKEN_PARAM" in stack
    assert 'resource "aws_dynamodb_table" "gemini_key_health"' in stack


def test_api_lambda_can_construct_the_hosted_embedding_provider():
    stack = pathlib.Path("infra/terraform/app/main.tf").read_text()
    api_policy = stack.split('data "aws_iam_policy_document" "api"', 1)[1].split(
        'resource "aws_iam_role_policy" "api"', 1
    )[0]
    api_lambda = stack.split('resource "aws_lambda_function" "api"', 1)[1].split(
        'resource "aws_lambda_function" "worker"', 1
    )[0]

    assert "local.parameter_arns.gemini" in api_policy
    assert "dynamodb:BatchGetItem" in api_policy
    assert "dynamodb:UpdateItem" in api_policy
    assert "bedrock:InvokeModel" in api_policy
    assert "HINDSIGHT_GEMINI_API_KEYS_PARAM" in api_lambda
    assert "HINDSIGHT_GEMINI_KEY_HEALTH_TABLE" in api_lambda
    assert "LLM_PROVIDER" in api_lambda
    assert "EMBEDDING_PROVIDER" in api_lambda
    assert "GEMINI_EMBEDDING_MODEL" in api_lambda
    assert "BEDROCK_EMBEDDING_MODEL" in api_lambda
    assert 'resource "aws_cloudwatch_event_rule" "operation_reaper"' in stack
    assert 'command = "reap_memory_operations"' in stack


def test_destroy_workflow_pauses_changefeed_and_requires_confirmation():
    workflow = pathlib.Path(".github/workflows/destroy-demo.yml").read_text()

    assert "destroy-demo" in workflow
    assert "configure_changefeed.py pause" in workflow
    assert "environment: demo" in workflow
    assert "CLOUDFLARE_API_TOKEN" in workflow
    assert "plan -destroy" in workflow


def test_bootstrap_prerequisites_are_isolated_and_oidc_is_narrow():
    bootstrap = pathlib.Path("infra/terraform/bootstrap/main.tf").read_text()

    assert "prevent_destroy = true" in bootstrap
    assert "aws_ssm_parameter" not in bootstrap
    assert 'data "aws_s3_bucket" "state"' in bootstrap
    assert 'test     = "StringEquals"' in bootstrap
    assert "cloudfront:*" not in bootstrap
    assert "dynamodb:*" not in bootstrap
    assert "s3:*" not in bootstrap
    assert "events:PutRule" in bootstrap
    assert "events:PutTargets" in bootstrap
    assert "bedrock:InvokeModel" in bootstrap
    assert "foundation-model/${var.bedrock_embedding_model}" in bootstrap


def test_deploy_preflights_dependencies_and_invalidates_cloudfront():
    workflow = pathlib.Path(".github/workflows/deploy-demo.yml").read_text()

    assert "deployment_preflight.py" in workflow
    assert "reembed_memories.py" in workflow
    assert "create-invalidation" in workflow
    assert "environment: demo" in workflow
    assert "CLOUDFLARE_API_TOKEN" in workflow
    assert workflow.index("scripts/migrate.py") < workflow.index(
        "terraform -chdir=infra/terraform/app apply"
    )
    assert 'export EMBEDDING_PROVIDER="$TF_VAR_embedding_provider"' in workflow
    assert "export EMBEDDING_PROVIDER=gemini" not in workflow
    assert "github.triggering_actor" in workflow
    assert '"$TRIGGERING_ACTOR" == "$REPOSITORY_OWNER"' in workflow
