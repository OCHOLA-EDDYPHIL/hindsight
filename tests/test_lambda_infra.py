"""Static checks for the Terraform-owned Lambda deployment."""

import pathlib
import re


def test_gemini_reasoning_default_is_consistent_across_runtime_and_deployment():
    from hindsight.reasoning import DEFAULT_GEMINI_MODEL

    assert DEFAULT_GEMINI_MODEL == "gemini-3.1-flash-lite"
    for path in (
        pathlib.Path(".env.example"),
        pathlib.Path("infra/terraform/app/variables.tf"),
        pathlib.Path(".github/workflows/deploy-demo.yml"),
        pathlib.Path(".github/workflows/live-acceptance.yml"),
    ):
        content = path.read_text()
        assert DEFAULT_GEMINI_MODEL in content
        assert "gemini-2.5-flash" not in content


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


def test_runtime_lambdas_use_distinct_database_parameters_without_bedrock():
    stack = pathlib.Path("infra/terraform/app/main.tf").read_text()
    api_policy = stack.split('data "aws_iam_policy_document" "api"', 1)[1].split(
        'resource "aws_iam_role_policy" "api"', 1
    )[0]
    api_lambda = stack.split('resource "aws_lambda_function" "api"', 1)[1].split(
        'resource "aws_lambda_function" "worker"', 1
    )[0]
    worker_policy = stack.split('data "aws_iam_policy_document" "worker"', 1)[1].split(
        'resource "aws_iam_role_policy" "worker"', 1
    )[0]

    assert "local.parameter_arns.gemini" in api_policy
    assert "dynamodb:BatchGetItem" in api_policy
    assert "dynamodb:UpdateItem" in api_policy
    assert "local.parameter_arns.api_database" in api_policy
    assert "local.parameter_arns.worker_database" not in api_policy
    assert "bedrock:" not in api_policy
    assert "local.parameter_arns.worker_database" in worker_policy
    assert "local.parameter_arns.api_database" not in worker_policy
    assert "bedrock:" not in worker_policy
    assert "HINDSIGHT_GEMINI_API_KEYS_PARAM" in api_lambda
    assert "HINDSIGHT_GEMINI_KEY_HEALTH_TABLE" in api_lambda
    assert "LLM_PROVIDER" in api_lambda
    assert "EMBEDDING_PROVIDER" in api_lambda
    assert "GEMINI_EMBEDDING_MODEL" in api_lambda
    assert "BEDROCK_" not in api_lambda
    assert 'resource "aws_cloudwatch_event_rule" "operation_reaper"' in stack
    assert 'command = "reap_memory_operations"' in stack
    assert 'resource "aws_api_gateway_account" "cloudwatch"' in stack
    assert "AmazonAPIGatewayPushToCloudWatchLogs" in stack
    assert "apigateway.amazonaws.com" in stack


def test_run_dispatch_outbox_has_scheduled_worker_and_narrow_queue_permissions():
    stack = pathlib.Path("infra/terraform/app/main.tf").read_text()
    api_policy = stack.split('data "aws_iam_policy_document" "api"', 1)[1].split(
        'resource "aws_iam_role_policy" "api"', 1
    )[0]
    worker_policy = stack.split('data "aws_iam_policy_document" "worker"', 1)[1].split(
        'resource "aws_iam_role_policy" "worker"', 1
    )[0]
    worker_lambda = stack.split('resource "aws_lambda_function" "worker"', 1)[1].split(
        'resource "aws_lambda_function" "websocket"', 1
    )[0]

    assert api_policy.count('"sqs:SendMessage"') == 1
    assert api_policy.count("aws_sqs_queue.runs.arn") == 1
    assert worker_policy.count('"sqs:SendMessage"') == 1
    assert worker_policy.count("aws_sqs_queue.runs.arn") == 2
    assert worker_policy.count("aws_sqs_queue.run_dlq.arn") == 1
    assert "HINDSIGHT_RUN_QUEUE_URL" in worker_lambda
    assert "HINDSIGHT_RUN_DLQ_ARN" in worker_lambda
    assert "HINDSIGHT_RUN_ATTEMPT_LEASE_SECONDS" in worker_lambda
    assert "HINDSIGHT_RUN_MAX_ATTEMPTS" in worker_lambda
    assert "var.validation_mode ? 30 : 180" in stack
    assert "var.validation_mode ? 60 : 300" in stack
    assert "var.validation_mode ? 180 : 360" in stack
    assert re.search(r"run_max_attempts\s*= 3", stack)
    assert 'resource "aws_lambda_event_source_mapping" "worker_dlq"' in stack
    assert 'resource "aws_cloudwatch_event_rule" "run_dispatcher"' in stack
    assert 'command = "dispatch_run_commands"' in stack
    assert 'resource "aws_lambda_permission" "run_dispatcher"' in stack


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
    assert "resources = local.lambda_function_arns" in version_refresh
    assert "lambda:ListVersionsByFunction" not in application_lifecycle
    assert "function:hindsight-${var.stage}-${component}" in bootstrap
    assert "bedrock:" not in bootstrap
    assert "BEDROCK_" not in bootstrap
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
    migration = workflow.index("scripts/migrate.py")
    persistence = workflow.index("scripts/initialize_agent_storage.py")
    roles = workflow.index("scripts/apply_database_roles.py")
    application = workflow.index("terraform -chdir=infra/terraform/app apply")
    assert migration < persistence < roles < application
    assert "/hindsight/demo/database-url" in workflow
    assert "/hindsight/demo/api-database-url" in workflow
    assert "/hindsight/demo/worker-database-url" in workflow
    assert "TF_VAR_database_url_parameter_name" not in workflow
    assert "BEDROCK" not in workflow
    assert 'export EMBEDDING_PROVIDER="$TF_VAR_embedding_provider"' in workflow
    assert "export EMBEDDING_PROVIDER=gemini" not in workflow
    assert "github.triggering_actor" in workflow
    assert '"$TRIGGERING_ACTOR" == "$REPOSITORY_OWNER"' in workflow


def test_deploy_health_only_is_owner_authorized_exact_main_and_checks_every_endpoint():
    workflow = pathlib.Path(".github/workflows/deploy-demo.yml").read_text()

    manual_authorization = workflow.split(
        '\n          if [[ "$EVENT_NAME" == "workflow_dispatch" ]]', 1
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
    assert 'f"{sys.argv[2].rstrip(\'/\')}/v1/realtime/ticket"' in workflow
    assert "urlencode({'ticket': ticket})" in workflow
    assert "Exact revision \\`$DEPLOYED_SHA\\` passed" in workflow


def test_deploy_uses_the_caller_authorized_source_revision():
    workflow = pathlib.Path(".github/workflows/deploy-demo.yml").read_text()
    authorization = workflow.split("      - id: authorize\n", 1)[1].split("  plan:\n", 1)[0]
    validation_branch = authorization.split('if [[ "$VALIDATION_MODE" == "true" ]]', 1)[1].split(
        '\n          if [[ "$EVENT_NAME" == "workflow_dispatch" ]]', 1
    )[0]

    assert "source_sha:" in workflow.split("  workflow_call:\n", 1)[1].split("    outputs:\n", 1)[0]
    assert (
        "required: true"
        in workflow.split("      source_sha:\n", 1)[1].split("    outputs:\n", 1)[0]
    )
    assert '"$REQUESTED_HEALTH_ONLY" == "false"' in validation_branch
    assert (
        '"$CALLER_WORKFLOW_REF" == "$REPOSITORY/.github/workflows/live-acceptance.yml@$REF_NAME"'
    ) in validation_branch
    assert '"$EVENT_NAME" == "workflow_dispatch"' in validation_branch
    assert '"$REF_NAME" == "refs/heads/main"' in validation_branch
    assert '"$REQUESTED_SOURCE_SHA" == "$EVENT_SHA"' in validation_branch
    assert "PR_HEAD_SHA" not in authorization
    assert authorization.count("=~ ^[0-9a-f]{40}$") == 2
    assert (
        '"$CALLER_WORKFLOW_REF" == "$REPOSITORY/.github/workflows/deploy-demo.yml@$REF_NAME"'
    ) in authorization
    assert workflow.count("ref: ${{ needs.authorize.outputs.source_sha }}") == 2
    assert workflow.count("EXPECTED_SHA: ${{ needs.authorize.outputs.source_sha }}") == 2
    assert "DEPLOYED_SHA: ${{ needs.authorize.outputs.source_sha }}" in workflow
    assert "demo-plan-${SOURCE_SHA}-${GITHUB_RUN_ID}" in workflow
    assert "github.event.pull_request.head.sha || github.sha" not in workflow
