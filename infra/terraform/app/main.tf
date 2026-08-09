locals {
  name = "${var.project_name}-${var.stage}"

  worker_timeout_seconds        = var.validation_mode ? 30 : 180
  run_attempt_lease_seconds     = var.validation_mode ? 60 : 300
  run_queue_visibility_seconds  = var.validation_mode ? 180 : 1080
  run_max_attempts              = 3
  run_dispatch_schedule         = "rate(1 minute)"
  run_dispatch_schedule_seconds = 60
  operation_poll_seconds        = (local.run_max_attempts - 1) * local.run_queue_visibility_seconds + local.worker_timeout_seconds + 60
  changefeed_timeout_seconds    = 30
  changefeed_lease_seconds      = 60

  api_zip      = var.api_zip_path != null ? var.api_zip_path : "${path.module}/../../../build/lambda-artifacts/hindsight-api.zip"
  worker_zip   = var.worker_zip_path != null ? var.worker_zip_path : "${path.module}/../../../build/lambda-artifacts/hindsight-worker.zip"
  realtime_zip = var.realtime_zip_path != null ? var.realtime_zip_path : "${path.module}/../../../build/lambda-artifacts/hindsight-realtime.zip"
  web_root     = "${path.module}/../../../src/hindsight/web"

  cloudfront_aliases = var.cloudfront_aliases != null ? var.cloudfront_aliases : (
    var.domain_name == null ? [] : [var.domain_name]
  )
  public_origin = var.public_origin != null ? var.public_origin : (
    var.domain_name == null ? "" : "https://${var.domain_name}"
  )

  lambda_artifacts = {
    api      = local.api_zip
    worker   = local.worker_zip
    realtime = local.realtime_zip
  }

  parameter_arns = {
    api_database    = "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${var.api_database_url_parameter_name}"
    worker_database = "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${var.worker_database_url_parameter_name}"
    gemini          = "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${var.gemini_api_keys_parameter_name}"
    operator        = "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${var.operator_token_parameter_name}"
    changefeed      = "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${var.changefeed_token_parameter_name}"
  }

  content_types = {
    ".css"  = "text/css; charset=utf-8"
    ".html" = "text/html; charset=utf-8"
    ".js"   = "text/javascript; charset=utf-8"
    ".svg"  = "image/svg+xml"
  }
}

check "custom_domain_configuration" {
  assert {
    condition = (
      length(local.cloudfront_aliases) == 0 ||
      var.acm_certificate_arn != null
    )
    error_message = "CloudFront aliases require acm_certificate_arn."
  }

  assert {
    condition = (
      !var.manage_public_dns ||
      (
        var.domain_name != null &&
        var.cloudflare_zone_id != null &&
        contains(local.cloudfront_aliases, var.domain_name)
      )
    )
    error_message = "Managed public DNS requires domain_name, cloudflare_zone_id, and a matching CloudFront alias."
  }
}

resource "aws_s3_bucket" "artifacts" {
  bucket        = "${local.name}-${data.aws_caller_identity.current.account_id}-${var.aws_region}-artifacts"
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "lambda_artifact" {
  for_each = local.lambda_artifacts

  bucket      = aws_s3_bucket.artifacts.id
  key         = "lambda/${each.key}.zip"
  source      = each.value
  source_hash = try(filebase64sha256(each.value), "artifact-not-built")
}

resource "aws_s3_bucket" "ui" {
  bucket        = "${local.name}-${data.aws_caller_identity.current.account_id}-${var.aws_region}-ui"
  force_destroy = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ui" {
  bucket = aws_s3_bucket.ui.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "ui" {
  bucket                  = aws_s3_bucket.ui.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "ui_asset" {
  for_each = {
    for file in fileset(local.web_root, "**") : file => file
    if file != "config.js"
  }

  bucket        = aws_s3_bucket.ui.id
  key           = each.key
  source        = "${local.web_root}/${each.value}"
  etag          = filemd5("${local.web_root}/${each.value}")
  content_type  = lookup(local.content_types, regex("(\\.[^.]+)$", each.value)[0], "application/octet-stream")
  cache_control = each.key == "index.html" ? "no-cache" : "public, max-age=31536000, immutable"
}

resource "aws_s3_object" "ui_config" {
  bucket        = aws_s3_bucket.ui.id
  key           = "config.js"
  content_type  = "text/javascript; charset=utf-8"
  cache_control = "no-cache"
  content = <<-JS
    window.HINDSIGHT_CONFIG = ${jsonencode({
  apiBase              = "/v1"
  snapshotBase         = null
  websocketUrl         = "${replace(aws_apigatewayv2_api.websocket.api_endpoint, "https://", "wss://")}/${aws_apigatewayv2_stage.websocket.name}"
  defaultNamespace     = "demo:payments-poison-rewind"
  pollIntervalMs       = 4000
  operationPollSeconds = local.operation_poll_seconds
})};
  JS
}

resource "aws_sqs_queue" "run_dlq" {
  name                       = "${local.name}-run-dlq"
  visibility_timeout_seconds = local.run_queue_visibility_seconds
  message_retention_seconds  = 1209600
  sqs_managed_sse_enabled    = true
}

resource "aws_sqs_queue" "runs" {
  name                       = "${local.name}-runs"
  visibility_timeout_seconds = local.run_queue_visibility_seconds
  message_retention_seconds  = 1209600
  receive_wait_time_seconds  = 10
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.run_dlq.arn
    maxReceiveCount     = local.run_max_attempts
  })
}

resource "aws_dynamodb_table" "connections" {
  name         = "${local.name}-websocket-connections"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "connection_id"

  attribute {
    name = "connection_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption { enabled = true }
}

resource "aws_dynamodb_table" "subscriptions" {
  name         = "${local.name}-websocket-subscriptions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "topic_key"
  range_key    = "connection_id"

  attribute {
    name = "topic_key"
    type = "S"
  }

  attribute {
    name = "connection_id"
    type = "S"
  }

  global_secondary_index {
    name            = "connection-id-index"
    hash_key        = "connection_id"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption { enabled = true }
}

resource "aws_dynamodb_table" "changefeed_idempotency" {
  name         = "${local.name}-changefeed-idempotency"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "event_id"

  attribute {
    name = "event_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption { enabled = true }
}

resource "aws_dynamodb_table" "gemini_key_health" {
  name         = "${local.name}-gemini-key-health"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "slot_id"

  attribute {
    name = "slot_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption { enabled = true }
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "apigateway_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["apigateway.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "apigateway_cloudwatch" {
  name               = "${local.name}-apigateway-cloudwatch"
  assume_role_policy = data.aws_iam_policy_document.apigateway_assume.json
}

resource "aws_iam_role_policy_attachment" "apigateway_cloudwatch" {
  role       = aws_iam_role.apigateway_cloudwatch.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

resource "aws_api_gateway_account" "cloudwatch" {
  cloudwatch_role_arn = aws_iam_role.apigateway_cloudwatch.arn

  depends_on = [aws_iam_role_policy_attachment.apigateway_cloudwatch]
}

resource "aws_iam_role" "api" {
  name               = "${local.name}-api"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role" "worker" {
  name               = "${local.name}-worker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role" "websocket" {
  name               = "${local.name}-websocket"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role" "changefeed" {
  name               = "${local.name}-changefeed"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "basic_logs" {
  for_each = {
    api        = aws_iam_role.api.name
    worker     = aws_iam_role.worker.name
    websocket  = aws_iam_role.websocket.name
    changefeed = aws_iam_role.changefeed.name
  }

  role       = each.value
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "api" {
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [local.parameter_arns.api_database, local.parameter_arns.gemini, local.parameter_arns.operator]
  }
  statement {
    actions = [
      "dynamodb:BatchGetItem",
      "dynamodb:DeleteItem",
      "dynamodb:UpdateItem"
    ]
    resources = [aws_dynamodb_table.gemini_key_health.arn]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.runs.arn]
  }
}

resource "aws_iam_role_policy" "api" {
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

data "aws_iam_policy_document" "worker" {
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [local.parameter_arns.worker_database, local.parameter_arns.gemini]
  }
  statement {
    actions = [
      "dynamodb:BatchGetItem",
      "dynamodb:DeleteItem",
      "dynamodb:UpdateItem"
    ]
    resources = [aws_dynamodb_table.gemini_key_health.arn]
  }
  statement {
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes"
    ]
    resources = [aws_sqs_queue.runs.arn, aws_sqs_queue.run_dlq.arn]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.runs.arn]
  }
  statement {
    actions   = ["cloudwatch:GetMetricStatistics"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "worker" {
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

data "aws_iam_policy_document" "websocket" {
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [local.parameter_arns.operator]
  }
  statement {
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem"
    ]
    resources = [aws_dynamodb_table.connections.arn]
  }
  statement {
    actions = ["dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:Query"]
    resources = [
      aws_dynamodb_table.subscriptions.arn,
      "${aws_dynamodb_table.subscriptions.arn}/index/connection-id-index"
    ]
  }
}

resource "aws_iam_role_policy" "websocket" {
  role   = aws_iam_role.websocket.id
  policy = data.aws_iam_policy_document.websocket.json
}

data "aws_iam_policy_document" "changefeed" {
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [local.parameter_arns.changefeed]
  }
  statement {
    actions   = ["dynamodb:DeleteItem"]
    resources = [aws_dynamodb_table.connections.arn]
  }
  statement {
    actions = ["dynamodb:Query", "dynamodb:DeleteItem"]
    resources = [
      aws_dynamodb_table.subscriptions.arn,
      "${aws_dynamodb_table.subscriptions.arn}/index/connection-id-index"
    ]
  }
  statement {
    actions = [
      "dynamodb:DeleteItem",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem"
    ]
    resources = [
      aws_dynamodb_table.changefeed_idempotency.arn
    ]
  }
  statement {
    actions   = ["execute-api:ManageConnections"]
    resources = ["${aws_apigatewayv2_api.websocket.execution_arn}/${var.stage}/POST/@connections/*"]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.runs.arn]
  }
}

resource "aws_iam_role_policy" "changefeed" {
  role   = aws_iam_role.changefeed.id
  policy = data.aws_iam_policy_document.changefeed.json
}

resource "aws_lambda_function" "api" {
  function_name = "${local.name}-api"
  role          = aws_iam_role.api.arn
  runtime       = "python3.12"
  handler       = "hindsight.api.handler"
  memory_size   = 512
  timeout       = 30
  architectures = ["x86_64"]

  s3_bucket        = aws_s3_bucket.artifacts.id
  s3_key           = aws_s3_object.lambda_artifact["api"].key
  source_code_hash = try(filebase64sha256(local.api_zip), null)

  reserved_concurrent_executions = 5

  environment {
    variables = {
      HINDSIGHT_DATABASE_URL_PARAM        = var.api_database_url_parameter_name
      HINDSIGHT_DEPLOYED_REVISION         = var.deployed_revision
      HINDSIGHT_FUNCTION_AUTH_TOKEN_PARAM = var.operator_token_parameter_name
      HINDSIGHT_GEMINI_API_KEYS_PARAM     = var.gemini_api_keys_parameter_name
      HINDSIGHT_GEMINI_KEY_HEALTH_TABLE   = aws_dynamodb_table.gemini_key_health.name
      HINDSIGHT_RUN_QUEUE_URL             = aws_sqs_queue.runs.url
      HINDSIGHT_ALLOWED_ORIGINS           = local.public_origin
      HINDSIGHT_REQUIRE_TENANT_CONTEXT    = "1"
      HINDSIGHT_SECURE_COOKIES            = "1"
      LLM_PROVIDER                        = var.llm_provider
      EMBEDDING_PROVIDER                  = var.embedding_provider
      GEMINI_EMBEDDING_MODEL              = var.gemini_embedding_model
      HINDSIGHT_GEMINI_REPRESENTATION     = var.gemini_embedding_representation
    }
  }
}

resource "aws_lambda_function" "worker" {
  function_name = "${local.name}-worker"
  role          = aws_iam_role.worker.arn
  runtime       = "python3.12"
  handler       = "hindsight.worker.handler"
  memory_size   = 1024
  timeout       = local.worker_timeout_seconds
  architectures = ["x86_64"]

  s3_bucket        = aws_s3_bucket.artifacts.id
  s3_key           = aws_s3_object.lambda_artifact["worker"].key
  source_code_hash = try(filebase64sha256(local.worker_zip), null)

  reserved_concurrent_executions = 2

  lifecycle {
    precondition {
      condition = (
        local.worker_timeout_seconds < local.run_attempt_lease_seconds &&
        local.run_attempt_lease_seconds < local.run_queue_visibility_seconds &&
        (
          !var.validation_mode ||
          local.run_queue_visibility_seconds >= local.worker_timeout_seconds * 6
        )
      )
      error_message = "Worker timeout, attempt lease, and queue visibility must remain safely ordered, with six-times visibility in validation mode."
    }
  }

  environment {
    variables = {
      HINDSIGHT_DATABASE_URL_PARAM        = var.worker_database_url_parameter_name
      HINDSIGHT_GEMINI_API_KEYS_PARAM     = var.gemini_api_keys_parameter_name
      HINDSIGHT_GEMINI_KEY_HEALTH_TABLE   = aws_dynamodb_table.gemini_key_health.name
      HINDSIGHT_RUN_QUEUE_URL             = aws_sqs_queue.runs.url
      HINDSIGHT_RUN_DLQ_ARN               = aws_sqs_queue.run_dlq.arn
      HINDSIGHT_RUN_MAX_ATTEMPTS          = tostring(local.run_max_attempts)
      HINDSIGHT_RUN_ATTEMPT_LEASE_SECONDS = tostring(local.run_attempt_lease_seconds)
      HINDSIGHT_AWS_ACCOUNT_ID            = data.aws_caller_identity.current.account_id
      HINDSIGHT_STAGE                     = var.stage
      HINDSIGHT_REQUIRE_TENANT_CONTEXT    = "1"
      HINDSIGHT_WORKER_TENANT_ID          = "00000000-0000-0000-0000-000000000002"
      LLM_PROVIDER                        = var.llm_provider
      EMBEDDING_PROVIDER                  = var.embedding_provider
      GEMINI_MODEL                        = var.gemini_model
      GEMINI_EMBEDDING_MODEL              = var.gemini_embedding_model
      HINDSIGHT_GEMINI_REPRESENTATION     = var.gemini_embedding_representation
      REASONING_MAX_ATTEMPTS              = tostring(var.reasoning_max_attempts)
    }
  }
}

resource "aws_lambda_function" "websocket" {
  function_name = "${local.name}-websocket"
  role          = aws_iam_role.websocket.arn
  runtime       = "python3.12"
  handler       = "hindsight.realtime.websocket_handler"
  memory_size   = 256
  timeout       = 10

  s3_bucket        = aws_s3_bucket.artifacts.id
  s3_key           = aws_s3_object.lambda_artifact["realtime"].key
  source_code_hash = try(filebase64sha256(local.realtime_zip), null)

  reserved_concurrent_executions = 5

  environment {
    variables = {
      HINDSIGHT_WEBSOCKET_CONNECTION_TABLE   = aws_dynamodb_table.connections.name
      HINDSIGHT_WEBSOCKET_SUBSCRIPTION_TABLE = aws_dynamodb_table.subscriptions.name
      HINDSIGHT_REALTIME_TICKET_SECRET_PARAM = var.operator_token_parameter_name
    }
  }
}

resource "aws_lambda_function" "changefeed" {
  function_name = "${local.name}-changefeed"
  role          = aws_iam_role.changefeed.arn
  runtime       = "python3.12"
  handler       = "hindsight.realtime.changefeed_handler"
  memory_size   = 256
  timeout       = local.changefeed_timeout_seconds

  s3_bucket        = aws_s3_bucket.artifacts.id
  s3_key           = aws_s3_object.lambda_artifact["realtime"].key
  source_code_hash = try(filebase64sha256(local.realtime_zip), null)

  reserved_concurrent_executions = 5

  environment {
    variables = {
      HINDSIGHT_WEBSOCKET_CONNECTION_TABLE    = aws_dynamodb_table.connections.name
      HINDSIGHT_WEBSOCKET_SUBSCRIPTION_TABLE  = aws_dynamodb_table.subscriptions.name
      HINDSIGHT_WEBSOCKET_MANAGEMENT_ENDPOINT = "https://${aws_apigatewayv2_api.websocket.id}.execute-api.${var.aws_region}.amazonaws.com/${var.stage}"
      HINDSIGHT_CHANGEFEED_AUTH_TOKEN_PARAM   = var.changefeed_token_parameter_name
      HINDSIGHT_CHANGEFEED_IDEMPOTENCY_TABLE  = aws_dynamodb_table.changefeed_idempotency.name
      HINDSIGHT_CHANGEFEED_LEASE_SECONDS      = tostring(local.changefeed_lease_seconds)
      HINDSIGHT_RUN_QUEUE_URL                 = aws_sqs_queue.runs.url
    }
  }

  lifecycle {
    precondition {
      condition     = local.changefeed_timeout_seconds < local.changefeed_lease_seconds
      error_message = "The changefeed processing lease must outlive the Lambda timeout."
    }
  }
}

resource "aws_lambda_event_source_mapping" "worker" {
  event_source_arn        = aws_sqs_queue.runs.arn
  function_name           = aws_lambda_function.worker.arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]
  enabled                 = var.runtime_active

  scaling_config {
    maximum_concurrency = 2
  }
}

resource "aws_lambda_event_source_mapping" "worker_dlq" {
  event_source_arn        = aws_sqs_queue.run_dlq.arn
  function_name           = aws_lambda_function.worker.arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]
  enabled                 = var.runtime_active
}

resource "aws_cloudwatch_event_rule" "operation_reaper" {
  name                = "${local.name}-operation-reaper"
  description         = "Terminalize expired final governed-memory operation attempts"
  schedule_expression = local.run_dispatch_schedule
  state               = var.runtime_active ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "operation_reaper" {
  rule      = aws_cloudwatch_event_rule.operation_reaper.name
  target_id = "memory-operation-reaper"
  arn       = aws_lambda_function.worker.arn
  input     = jsonencode({ command = "reap_memory_operations" })
}

resource "aws_lambda_permission" "operation_reaper" {
  statement_id  = "AllowOperationReaper"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.worker.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.operation_reaper.arn
}

resource "aws_cloudwatch_event_rule" "run_dispatcher" {
  name                = "${local.name}-run-dispatcher"
  description         = "Dispatch pending and expired agent-run outbox commands"
  schedule_expression = local.run_dispatch_schedule
  state               = var.runtime_active ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "run_dispatcher" {
  rule      = aws_cloudwatch_event_rule.run_dispatcher.name
  target_id = "agent-run-dispatcher"
  arn       = aws_lambda_function.worker.arn
  input     = jsonencode({ command = "dispatch_run_commands" })
}

resource "aws_lambda_permission" "run_dispatcher" {
  statement_id  = "AllowRunDispatcher"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.worker.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.run_dispatcher.arn
}

resource "aws_apigatewayv2_api" "http" {
  name          = "${local.name}-http"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 30000
}

resource "aws_apigatewayv2_integration" "changefeed" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.changefeed.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 30000
}

resource "aws_apigatewayv2_route" "api_root" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "ANY /v1"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_route" "api_proxy" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "ANY /v1/{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_route" "api_v2_root" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "ANY /v2"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_route" "api_v2_proxy" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "ANY /v2/{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_route" "changefeed" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "POST /internal/changefeed"
  target    = "integrations/${aws_apigatewayv2_integration.changefeed.id}"
}

resource "aws_cloudwatch_log_group" "http_access" {
  name              = "/aws/apigateway/${local.name}-http"
  retention_in_days = var.log_retention_days
}

resource "aws_apigatewayv2_stage" "http" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.http_access.arn
    format = jsonencode({
      requestId        = "$context.requestId"
      routeKey         = "$context.routeKey"
      status           = "$context.status"
      responseLength   = "$context.responseLength"
      integrationError = "$context.integrationErrorMessage"
    })
  }

  depends_on = [aws_api_gateway_account.cloudwatch]
}

resource "aws_lambda_permission" "http_api" {
  statement_id  = "AllowHttpApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*/v1*"
}

resource "aws_lambda_permission" "http_api_v2" {
  statement_id  = "AllowHttpApiV2"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*/v2*"
}

resource "aws_lambda_permission" "http_changefeed" {
  statement_id  = "AllowChangefeedRoute"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.changefeed.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/POST/internal/changefeed"
}

resource "aws_apigatewayv2_api" "websocket" {
  name                       = "${local.name}-websocket"
  protocol_type              = "WEBSOCKET"
  route_selection_expression = "$request.body.type"
}

resource "aws_apigatewayv2_integration" "websocket" {
  api_id             = aws_apigatewayv2_api.websocket.id
  integration_type   = "AWS_PROXY"
  integration_method = "POST"
  integration_uri    = aws_lambda_function.websocket.invoke_arn
}

resource "aws_apigatewayv2_route" "websocket" {
  for_each = toset(["$connect", "$disconnect", "$default", "subscribe", "unsubscribe", "ping"])

  api_id    = aws_apigatewayv2_api.websocket.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.websocket.id}"
}

resource "aws_cloudwatch_log_group" "websocket_access" {
  name              = "/aws/apigateway/${local.name}-websocket"
  retention_in_days = var.log_retention_days
}

resource "aws_apigatewayv2_stage" "websocket" {
  api_id      = aws_apigatewayv2_api.websocket.id
  name        = var.stage
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 30
    throttling_rate_limit  = 15
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.websocket_access.arn
    format = jsonencode({
      requestId    = "$context.requestId"
      routeKey     = "$context.routeKey"
      status       = "$context.status"
      connectionId = "$context.connectionId"
    })
  }

  depends_on = [aws_api_gateway_account.cloudwatch]
}

resource "aws_lambda_permission" "websocket" {
  statement_id  = "AllowWebSocketApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.websocket.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.websocket.execution_arn}/*"
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each = {
    api        = aws_lambda_function.api.function_name
    worker     = aws_lambda_function.worker.function_name
    websocket  = aws_lambda_function.websocket.function_name
    changefeed = aws_lambda_function.changefeed.function_name
  }

  name              = "/aws/lambda/${each.value}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudfront_origin_access_control" "ui" {
  name                              = "${local.name}-ui"
  description                       = "Private Hindsight UI bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "ui" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"
  price_class         = "PriceClass_100"
  aliases             = local.cloudfront_aliases

  origin {
    domain_name              = aws_s3_bucket.ui.bucket_regional_domain_name
    origin_id                = "ui-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.ui.id
  }

  origin {
    domain_name = replace(aws_apigatewayv2_api.http.api_endpoint, "https://", "")
    origin_id   = "http-api"
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id       = "ui-s3"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD", "OPTIONS"]
    compress               = true
    forwarded_values {
      query_string = false
      cookies { forward = "none" }
    }
    min_ttl     = 0
    default_ttl = 300
    max_ttl     = 31536000
  }

  ordered_cache_behavior {
    path_pattern             = "/v1/*"
    target_origin_id         = "http-api"
    viewer_protocol_policy   = "https-only"
    allowed_methods          = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods           = ["GET", "HEAD", "OPTIONS"]
    compress                 = true
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id
  }

  ordered_cache_behavior {
    path_pattern             = "/v2/*"
    target_origin_id         = "http-api"
    viewer_protocol_policy   = "https-only"
    allowed_methods          = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods           = ["GET", "HEAD", "OPTIONS"]
    compress                 = true
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    cloudfront_default_certificate = length(local.cloudfront_aliases) == 0
    acm_certificate_arn            = length(local.cloudfront_aliases) > 0 ? var.acm_certificate_arn : null
    ssl_support_method             = length(local.cloudfront_aliases) > 0 ? "sni-only" : null
    minimum_protocol_version       = length(local.cloudfront_aliases) > 0 ? "TLSv1.2_2021" : "TLSv1"
  }
}

data "aws_iam_policy_document" "ui_bucket" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.ui.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.ui.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "ui" {
  bucket = aws_s3_bucket.ui.id
  policy = data.aws_iam_policy_document.ui_bucket.json
}

resource "cloudflare_dns_record" "ui" {
  count = var.manage_public_dns ? 1 : 0

  zone_id = var.cloudflare_zone_id
  name    = var.domain_name
  content = aws_cloudfront_distribution.ui.domain_name
  type    = "CNAME"
  ttl     = 1
  proxied = false
  comment = "Hindsight demo CloudFront alias"
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each = {
    api        = aws_lambda_function.api.function_name
    worker     = aws_lambda_function.worker.function_name
    websocket  = aws_lambda_function.websocket.function_name
    changefeed = aws_lambda_function.changefeed.function_name
  }

  alarm_name          = "${each.value}-errors"
  alarm_description   = "${each.value} emitted an unhandled error"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
  dimensions          = { FunctionName = each.value }
}

resource "aws_cloudwatch_metric_alarm" "run_dlq" {
  alarm_name          = "${local.name}-run-dlq-not-empty"
  alarm_description   = "An agent run exhausted its bounded retries"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
  dimensions          = { QueueName = aws_sqs_queue.run_dlq.name }
}
