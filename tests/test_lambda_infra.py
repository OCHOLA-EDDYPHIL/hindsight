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
    assert 'resource "aws_api_gateway_account" "cloudwatch"' in stack
    assert "AmazonAPIGatewayPushToCloudWatchLogs" in stack
    assert "apigateway.amazonaws.com" in stack


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
    assert "lambda:PutFunctionConcurrency" in bootstrap
    assert "lambda:DeleteFunctionConcurrency" in bootstrap
    version_refresh = bootstrap.split('sid       = "LambdaVersionRefresh"', 1)[1].split(
        "\n  statement {", 1
    )[0]
    application_lifecycle = bootstrap.split('sid = "ApplicationLifecycle"', 1)[1].split(
        "\n  statement {", 1
    )[0]
    assert bootstrap.count('"lambda:ListVersionsByFunction"') == 1
    assert 'lambda_version_refresh_actions = ["lambda:ListVersionsByFunction"]' in bootstrap
    assert "actions   = local.lambda_version_refresh_actions" in version_refresh
    assert 'resources = local.lambda_function_arns' in version_refresh
    assert "lambda:ListVersionsByFunction" not in application_lifecycle
    assert "function:hindsight-${var.stage}-${component}" in bootstrap
    assert "bedrock:InvokeModel" in bootstrap
    assert "foundation-model/${var.bedrock_embedding_model}" in bootstrap
    assert "s3:GetBucketAcl" in bootstrap
    assert "s3:GetBucketCORS" in bootstrap
    assert "s3:GetBucketOwnershipControls" in bootstrap
    assert "s3:GetBucketPolicyStatus" in bootstrap
    assert "s3:GetBucketWebsite" in bootstrap
    assert "s3:GetLifecycleConfiguration" in bootstrap
    assert "s3:GetReplicationConfiguration" in bootstrap
    assert "s3:GetObjectTagging" in bootstrap
    assert "s3:PutObjectTagging" in bootstrap
    assert "s3:DeleteObjectTagging" in bootstrap
    assert '"s3:Get*"' not in bootstrap
    assert "apigateway:TagResource" in bootstrap
    assert "apigateway:UntagResource" in bootstrap
    assert "cloudwatch:ListTagsForResource" in bootstrap
    assert "cloudwatch:TagResource" in bootstrap
    assert "cloudwatch:UntagResource" in bootstrap
    assert "logs:CreateLogDelivery" in bootstrap
    assert "logs:DeleteLogDelivery" in bootstrap
    assert "logs:GetLogDelivery" in bootstrap
    assert "logs:ListLogDeliveries" in bootstrap
    assert "logs:UpdateLogDelivery" in bootstrap
    assert "iam:ListInstanceProfilesForRole" in bootstrap
    assert "iam:UpdateAssumeRolePolicy" in bootstrap


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


def test_deploy_health_only_is_owner_authorized_exact_main_and_checks_every_endpoint():
    workflow = pathlib.Path(".github/workflows/deploy-demo.yml").read_text()

    manual_authorization = workflow.split(
        'if [[ "$EVENT_NAME" == "workflow_dispatch" ]]', 1
    )[1].split("exit 0", 1)[0]
    assert '"$REF_NAME" == "refs/heads/main"' in manual_authorization
    assert '"$ACTOR" == "$REPOSITORY_OWNER"' in manual_authorization
    assert '"$TRIGGERING_ACTOR" == "$REPOSITORY_OWNER"' in manual_authorization
    assert workflow.count("Verify exact source revision") == 2
    assert "timeout-minutes: 60" in workflow
    assert "if: inputs.health_only != true" in workflow
    assert "--connect-timeout 5 --max-time 15" in workflow
    assert 'direct API liveness" "$API_URL/v1/health/live' in workflow
    assert 'direct API readiness" "$API_URL/v1/health/ready' in workflow
    assert 'direct DB-backed route" "$API_URL/v1/incidents?limit=1' in workflow
    assert 'UI-proxied readiness" "$UI_URL/v1/health/ready' in workflow
    assert "from websockets.asyncio.client import connect" in workflow
    assert "Exact revision \\`$DEPLOYED_SHA\\` passed" in workflow
