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


def test_saved_plan_sources_do_not_capture_a_runner_checkout_path():
    stack = pathlib.Path("infra/terraform/app/main.tf").read_text()

    for artifact in ("api", "worker", "realtime"):
        assert f"abspath(var.{artifact}_zip_path)" not in stack
        assert (
            f'"${{path.module}}/../../../build/lambda-artifacts/hindsight-{artifact}.zip"' in stack
        )
    assert 'web_root     = "${path.module}/../../../src/hindsight/web"' in stack
    assert "web_root     = abspath(" not in stack


def test_runtime_lambdas_use_distinct_database_parameters():
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
    assert "local.parameter_arns.worker_database" in worker_policy
    assert "local.parameter_arns.api_database" not in worker_policy
    assert "HINDSIGHT_GEMINI_API_KEYS_PARAM" in api_lambda
    assert "HINDSIGHT_GEMINI_KEY_HEALTH_TABLE" in api_lambda
    assert "LLM_PROVIDER" in api_lambda
    assert "EMBEDDING_PROVIDER" in api_lambda
    assert "GEMINI_EMBEDDING_MODEL" in api_lambda
    assert "HINDSIGHT_GEMINI_REPRESENTATION" in api_lambda
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
    assert "var.validation_mode ? 180 : 1080" in stack
    assert re.search(r"run_max_attempts\s*= 3", stack)
    assert 'resource "aws_lambda_event_source_mapping" "worker_dlq"' in stack
    assert stack.count("message_retention_seconds  = 1209600") == 2
    assert stack.count("batch_size              = 1") == 2
    assert re.search(r"scaling_config\s*{\s*maximum_concurrency\s*= 2\s*}", stack)
    assert 'resource "aws_cloudwatch_event_rule" "run_dispatcher"' in stack
    assert 'command = "dispatch_run_commands"' in stack
    assert 'resource "aws_lambda_permission" "run_dispatcher"' in stack
    assert stack.count("enabled                 = var.runtime_active") == 2
    assert stack.count('state               = var.runtime_active ? "ENABLED" : "DISABLED"') == 2


def test_changefeed_relay_has_a_fenced_lease_and_narrow_idempotency_permissions():
    stack = pathlib.Path("infra/terraform/app/main.tf").read_text()
    changefeed_policy = stack.split('data "aws_iam_policy_document" "changefeed"', 1)[
        1
    ].split('resource "aws_iam_role_policy" "changefeed"', 1)[0]
    idempotency_statement = changefeed_policy.rsplit("  statement {", 3)[1]
    changefeed_lambda = stack.split('resource "aws_lambda_function" "changefeed"', 1)[
        1
    ].split('resource "aws_lambda_event_source_mapping" "worker"', 1)[0]

    assert 'changefeed_timeout_seconds    = 30' in stack
    assert 'changefeed_lease_seconds      = 60' in stack
    assert "timeout       = local.changefeed_timeout_seconds" in changefeed_lambda
    assert "HINDSIGHT_CHANGEFEED_LEASE_SECONDS" in changefeed_lambda
    assert "local.changefeed_timeout_seconds < local.changefeed_lease_seconds" in (
        changefeed_lambda
    )
    assert 'aws_dynamodb_table.changefeed_idempotency.arn' in idempotency_statement
    assert set(re.findall(r'"dynamodb:[A-Za-z]+"', idempotency_statement)) == {
        '"dynamodb:DeleteItem"',
        '"dynamodb:GetItem"',
        '"dynamodb:PutItem"',
        '"dynamodb:UpdateItem"',
    }


def test_product_identity_boundary_is_jwt_gated_with_opaque_realtime_tickets():
    stack = pathlib.Path("infra/terraform/app/main.tf").read_text()
    variables = pathlib.Path("infra/terraform/app/variables.tf").read_text()
    outputs = pathlib.Path("infra/terraform/app/outputs.tf").read_text()
    bootstrap = pathlib.Path("infra/terraform/bootstrap/main.tf").read_text()
    deploy = pathlib.Path(".github/workflows/deploy-demo.yml").read_text()
    destroy = pathlib.Path(".github/workflows/destroy-demo.yml").read_text()
    api_policy = stack.split('data "aws_iam_policy_document" "api"', 1)[1].split(
        'resource "aws_iam_role_policy" "api"', 1
    )[0]
    websocket_policy = stack.split('data "aws_iam_policy_document" "websocket"', 1)[
        1
    ].split('resource "aws_iam_role_policy" "websocket"', 1)[0]

    assert 'resource "aws_cognito_user_pool" "product"' in stack
    assert 'user_pool_tier = "LITE"' in stack
    assert "allow_admin_create_user_only = true" in stack
    assert "case_sensitive = false" in stack
    assert 'name     = "admin_only"' in stack
    assert "username_attributes" not in stack
    assert "auto_verified_attributes" not in stack
    assert 'allowed_oauth_flows                  = ["code"]' in stack
    assert "generate_secret                      = false" in stack
    assert '"ALLOW_ADMIN_USER_PASSWORD_AUTH"' in stack
    assert '"ALLOW_USER_PASSWORD_AUTH"' not in stack
    assert 'resource "aws_apigatewayv2_authorizer" "product"' in stack
    assert 'route_key = "GET /v1/{proxy+}"' in stack
    assert 'route_key = "POST /v1/realtime/ticket"' in stack
    assert stack.count('authorization_type = "JWT"') == 2
    assert stack.count('route_key = "OPTIONS /v2') == 2
    assert 'resource "aws_dynamodb_table" "realtime_tickets"' in stack
    assert 'hash_key     = "ticket_digest"' in stack
    assert 'name            = "tenant-id-index"' in stack
    assert 'sid       = "RealtimeTicketIssue"' in api_policy
    assert 'actions   = ["dynamodb:PutItem"]' in api_policy
    assert 'sid       = "RealtimeTicketRedeem"' in websocket_policy
    assert 'actions   = ["dynamodb:DeleteItem"]' in websocket_policy
    assert "HINDSIGHT_REALTIME_TICKET_SECRET_PARAM" not in stack
    assert "HINDSIGHT_FUNCTION_AUTH_TOKEN_PARAM" not in stack
    assert "operator_token_parameter_name" not in variables
    assert "publicApiBase" in stack
    assert "productApiBase" in stack
    assert 'output "cognito_authorize_endpoint"' in outputs
    assert 'output "cognito_logout_endpoint"' in outputs
    assert 'output "cognito_issuer"' in outputs
    assert 'output "cognito_hosted_ui_base_url"' in outputs
    assert 'variable "enable_waf"' in variables
    assert "count    = var.enable_waf ? 1 : 0" in stack
    assert "web_acl_id          = var.enable_waf" in stack
    assert "TF_VAR_enable_waf: ${{ vars.HINDSIGHT_ENABLE_WAF || 'false' }}" in deploy
    assert "TF_VAR_enable_waf: ${{ vars.HINDSIGHT_ENABLE_WAF || 'false' }}" in destroy
    assert "Verify product identity inputs" in deploy
    assert "Product identity password secrets are required" in deploy
    assert "Viewer and operator passwords must be distinct" in deploy
    assert "cognito-idp:AdminInitiateAuth" in bootstrap
    assert "wafv2:CreateWebACL" in bootstrap


def test_candidate_plane_separates_runtime_aliases_and_dns_ownership():
    stack = pathlib.Path("infra/terraform/app/main.tf").read_text()
    variables = pathlib.Path("infra/terraform/app/variables.tf").read_text()
    outputs = pathlib.Path("infra/terraform/app/outputs.tf").read_text()
    deploy = pathlib.Path(".github/workflows/deploy-demo.yml").read_text()
    ci = pathlib.Path(".github/workflows/ci.yml").read_text()

    for variable in (
        "runtime_active",
        "manage_public_dns",
        "public_origin",
        "cloudfront_aliases",
    ):
        assert f'variable "{variable}"' in variables
    assert "aliases             = local.cloudfront_aliases" in stack
    assert "count = var.manage_public_dns ? 1 : 0" in stack
    assert re.search(r"HINDSIGHT_ALLOWED_ORIGINS\s*= local\.public_origin", stack)
    assert 'output "cloudfront_distribution_domain_name"' in outputs
    assert 'output "runtime_active"' in outputs
    assert "TF_VAR_runtime_active" in deploy
    assert "TF_VAR_manage_public_dns" in deploy
    assert "TF_VAR_cloudfront_aliases" in deploy
    assert "inputs.health_only != true && env.TF_VAR_runtime_active == 'true'" in deploy
    embedding_step = deploy.split("      - name: Configure embedding profile\n", 1)[1].split(
        "      - name: Configure changefeed\n", 1
    )[0]
    assert "if: env.TF_VAR_runtime_active == 'true'" in embedding_step
    assert "scripts/reembed_memories.py" in embedding_step
    assert "inputs.health_only" not in embedding_step
    for command in ("init -backend=false -input=false", "validate", "test"):
        assert f"terraform -chdir=infra/terraform/edge {command}" in ci


def test_destroy_workflow_pauses_changefeed_and_requires_confirmation():
    workflow = pathlib.Path(".github/workflows/destroy-demo.yml").read_text()

    assert '"destroy-$DEPLOYMENT_ENVIRONMENT"' in workflow
    assert "configure_changefeed.py pause" in workflow
    assert "environment: ${{ inputs.deployment_environment }}" in workflow
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
    deploy_policy = bootstrap.split(
        'data "aws_iam_policy_document" "github_deploy" {', 1
    )[1].split('resource "aws_iam_role_policy" "github_deploy"', 1)[0]
    assert "s3:*" not in deploy_policy
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
    assert re.search(
        r'lambda_version_refresh_actions\s*=\s*\["lambda:ListVersionsByFunction"\]',
        bootstrap,
    )
    assert "actions   = local.lambda_version_refresh_actions" in version_refresh
    assert "resources = local.lambda_function_arns" in version_refresh
    assert "lambda:ListVersionsByFunction" not in application_lifecycle
    assert "function:hindsight-${var.stage}-${component}" in bootstrap
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
    controlled_write = bootstrap.split('sid       = "ControlledIncidentTelemetryWrite"', 1)[
        1
    ].split("\n  statement {", 1)[0]
    assert 'actions   = ["cloudwatch:PutMetricData"]' in controlled_write
    assert 'variable = "cloudwatch:namespace"' in controlled_write
    assert 'values   = ["Hindsight/ControlledIncidentTelemetry"]' in controlled_write
    controlled_read = bootstrap.split('sid       = "ControlledIncidentTelemetryRead"', 1)[1].split(
        "\n  statement {", 1
    )[0]
    assert 'actions   = ["cloudwatch:GetMetricStatistics"]' in controlled_read
    assert "logs:CreateLogDelivery" in bootstrap
    assert "logs:DeleteLogDelivery" in bootstrap
    assert "logs:GetLogDelivery" in bootstrap
    assert "logs:ListLogDeliveries" in bootstrap
    assert "logs:UpdateLogDelivery" in bootstrap
    assert "iam:ListInstanceProfilesForRole" in bootstrap
    assert "iam:UpdateAssumeRolePolicy" in bootstrap


def test_learning_evidence_archive_is_object_locked_and_narrowly_writable():
    bootstrap = pathlib.Path("infra/terraform/bootstrap/main.tf").read_text()
    outputs = pathlib.Path("infra/terraform/bootstrap/outputs.tf").read_text()

    bucket = bootstrap.split('resource "aws_s3_bucket" "learning_evidence"', 1)[1].split(
        'resource "aws_s3_bucket_versioning"', 1
    )[0]
    evidence_policy = bootstrap.split('data "aws_iam_policy_document" "github_evidence"', 1)[
        1
    ].split('resource "aws_iam_role_policy" "github_evidence"', 1)[0]
    deploy_deny = bootstrap.split('sid    = "EvidenceArchiveMutationDenied"', 1)[1].split(
        "\n  statement {", 1
    )[0]

    assert "object_lock_enabled = true" in bucket
    assert "prevent_destroy = true" in bucket
    assert 'mode  = "GOVERNANCE"' in bootstrap
    assert "years = 7" in bootstrap
    assert 'status = "Enabled"' in bootstrap
    assert 'sse_algorithm = "AES256"' in bootstrap
    assert 'name                 = "hindsight-github-evidence"' in bootstrap
    assert "local.evidence_parameter_arns" in evidence_policy
    assert '"s3:PutObject"' in evidence_policy
    assert '"s3:GetObjectRetention"' in evidence_policy
    assert "s3:DeleteObject" not in evidence_policy
    assert "s3:BypassGovernanceRetention" not in evidence_policy
    assert '"s3:DeleteObject"' in deploy_deny
    assert '"s3:BypassGovernanceRetention"' in deploy_deny
    assert 'output "github_evidence_role_arn"' in outputs
    assert 'output "learning_evidence_bucket"' in outputs


def test_protected_corpus_uses_rotating_kms_and_narrow_permissions():
    bootstrap = pathlib.Path("infra/terraform/bootstrap/main.tf").read_text()
    outputs = pathlib.Path("infra/terraform/bootstrap/outputs.tf").read_text()
    policy = bootstrap.split('data "aws_iam_policy_document" "github_evidence"', 1)[1].split(
        'resource "aws_iam_role_policy" "github_evidence"', 1
    )[0]

    assert 'resource "aws_kms_key" "learning_corpus"' in bootstrap
    assert "enable_key_rotation     = true" in bootstrap
    assert "prevent_destroy = true" in bootstrap
    assert "gemini-api-keys" in bootstrap
    assert 'actions   = ["ssm:GetParameter", "ssm:GetParameters"]' in policy
    assert 'sid = "ProtectedCorpusEncryption"' in policy
    assert 'output "learning_corpus_kms_key_arn"' in outputs


def test_qualification_hmac_key_is_non_exportable_and_evidence_role_scoped():
    bootstrap = pathlib.Path("infra/terraform/bootstrap/main.tf").read_text()
    outputs = pathlib.Path("infra/terraform/bootstrap/outputs.tf").read_text()

    key = bootstrap.split('resource "aws_kms_key" "learning_qualification_hmac"', 1)[1].split(
        'resource "aws_kms_alias" "learning_qualification_hmac"', 1
    )[0]
    policy = bootstrap.split('sid = "QualificationOpaqueIdentifiers"', 1)[1].split(
        "\n  statement {", 1
    )[0]

    assert 'key_usage                = "GENERATE_VERIFY_MAC"' in key
    assert 'customer_master_key_spec = "HMAC_256"' in key
    assert "prevent_destroy = true" in key
    assert "kms:GenerateMac" in policy
    assert "kms:VerifyMac" in policy
    assert "kms:Decrypt" not in policy
    assert 'output "learning_qualification_hmac_key_arn"' in outputs


def test_deploy_preflights_dependencies_and_invalidates_cloudfront():
    workflow = pathlib.Path(".github/workflows/deploy-demo.yml").read_text()

    assert "deployment_preflight.py" in workflow
    assert "reembed_memories.py" in workflow
    assert "create-invalidation" in workflow
    assert "environment: ${{ needs.authorize.outputs.deployment_environment }}" in workflow
    assert "scripts/deployment_identity_preflight.py" in workflow
    assert "EXPECTED_AWS_ACCOUNT_ID: ${{ vars.AWS_ACCOUNT_ID }}" in workflow
    assert "TF_STATE_KEY: ${{ vars.TF_STATE_KEY" in workflow
    assert '"demo" || "$REQUESTED_ENVIRONMENT" == "demo-candidate"' in workflow
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
    assert 'export EMBEDDING_PROVIDER="$TF_VAR_embedding_provider"' in workflow
    assert (
        'export HINDSIGHT_GEMINI_REPRESENTATION="$TF_VAR_gemini_embedding_representation"'
    ) in workflow
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
    assert "f\"{sys.argv[2].rstrip('/')}/v1/realtime/ticket\"" in workflow
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
    assert "${DEPLOYMENT_ENVIRONMENT}-plan-${SOURCE_SHA}-${GITHUB_RUN_ID}" in workflow
    assert "github.event.pull_request.head.sha || github.sha" not in workflow
