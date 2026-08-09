mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:iam::123456789012:user/terraform-test"
      user_id    = "terraform-test"
    }
  }

  mock_data "aws_partition" {
    defaults = {
      partition  = "aws"
      dns_suffix = "amazonaws.com"
    }
  }

  mock_data "aws_region" {
    defaults = {
      name = "us-east-1"
    }
  }

  mock_data "aws_cloudfront_cache_policy" {
    defaults = {
      id = "413f1600-0000-4000-8000-000000000000"
    }
  }

  mock_data "aws_cloudfront_origin_request_policy" {
    defaults = {
      id = "b689b0a8-0000-4000-8000-000000000000"
    }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

mock_provider "aws" {
  alias = "us_east_1"
}

mock_provider "cloudflare" {}

run "complete_demo_graph" {
  command = plan

  variables {
    api_zip_path        = "../../../src/hindsight/web/favicon.svg"
    worker_zip_path     = "../../../src/hindsight/web/favicon.svg"
    realtime_zip_path   = "../../../src/hindsight/web/favicon.svg"
    domain_name         = "hindsight.example.com"
    acm_certificate_arn = "arn:aws:acm:us-east-1:123456789012:certificate/test"
    cloudflare_zone_id  = "00000000000000000000000000000000"
    manage_public_dns   = true
  }

  assert {
    condition     = aws_sqs_queue.runs.redrive_policy != null
    error_message = "The run queue must retain a dead-letter policy."
  }

  assert {
    condition     = aws_lambda_function.worker.reserved_concurrent_executions == 2
    error_message = "Model-spend concurrency must remain bounded."
  }

  assert {
    condition = (
      aws_lambda_function.api.environment[0].variables.HINDSIGHT_DATABASE_URL_PARAM == var.api_database_url_parameter_name &&
      aws_lambda_function.worker.environment[0].variables.HINDSIGHT_DATABASE_URL_PARAM == var.worker_database_url_parameter_name &&
      var.api_database_url_parameter_name != var.worker_database_url_parameter_name
    )
    error_message = "API and worker functions must resolve distinct restricted database parameters."
  }

  assert {
    condition     = aws_lambda_function.api.environment[0].variables.HINDSIGHT_ALLOWED_ORIGINS == "https://hindsight.example.com"
    error_message = "The API must authorize browser requests from the configured CloudFront product origin."
  }

  assert {
    condition = (
      aws_lambda_function.worker.timeout == 180 &&
      tonumber(aws_lambda_function.worker.environment[0].variables.HINDSIGHT_RUN_ATTEMPT_LEASE_SECONDS) == 300 &&
      aws_sqs_queue.runs.visibility_timeout_seconds == 1080 &&
      tonumber(aws_lambda_function.worker.environment[0].variables.HINDSIGHT_RUN_MAX_ATTEMPTS) == 3 &&
      aws_lambda_function.worker.timeout < tonumber(aws_lambda_function.worker.environment[0].variables.HINDSIGHT_RUN_ATTEMPT_LEASE_SECONDS) &&
      tonumber(aws_lambda_function.worker.environment[0].variables.HINDSIGHT_RUN_ATTEMPT_LEASE_SECONDS) < aws_sqs_queue.runs.visibility_timeout_seconds &&
      aws_sqs_queue.run_dlq.visibility_timeout_seconds == aws_sqs_queue.runs.visibility_timeout_seconds
    )
    error_message = "Worker timeout, attempt lease, and both queue visibility windows must remain ordered."
  }

  assert {
    condition     = tonumber(aws_lambda_function.worker.environment[0].variables.HINDSIGHT_RUN_MAX_ATTEMPTS) == local.run_max_attempts
    error_message = "The database attempt budget and queue redrive budget must match."
  }

  assert {
    condition = (
      aws_sqs_queue.runs.message_retention_seconds == 1209600 &&
      aws_sqs_queue.run_dlq.message_retention_seconds == 1209600 &&
      aws_lambda_event_source_mapping.worker.batch_size == 1 &&
      aws_lambda_event_source_mapping.worker_dlq.batch_size == 1 &&
      aws_lambda_event_source_mapping.worker.scaling_config[0].maximum_concurrency == 2 &&
      toset(aws_lambda_event_source_mapping.worker.function_response_types) == toset(["ReportBatchItemFailures"]) &&
      toset(aws_lambda_event_source_mapping.worker_dlq.function_response_types) == toset(["ReportBatchItemFailures"])
    )
    error_message = "Both queues must retain messages for 14 days and isolate retries, while the source mapping stays within worker concurrency."
  }

  assert {
    condition = (
      aws_cloudwatch_event_rule.run_dispatcher.schedule_expression == "rate(1 minute)" &&
      aws_cloudwatch_event_rule.operation_reaper.schedule_expression == "rate(1 minute)" &&
      jsondecode(aws_cloudwatch_event_target.run_dispatcher.input).command == "dispatch_run_commands"
    )
    error_message = "The worker must sweep durable run commands through the run queue."
  }

  assert {
    condition     = aws_dynamodb_table.connections.ttl[0].enabled
    error_message = "WebSocket connection records must expire."
  }

  assert {
    condition = (
      aws_dynamodb_table.subscriptions.hash_key == "topic_key" &&
      aws_dynamodb_table.subscriptions.range_key == "connection_id" &&
      anytrue([
        for index in aws_dynamodb_table.subscriptions.global_secondary_index :
        index.hash_key == "connection_id" && index.name == "connection-id-index"
      ]) &&
      aws_dynamodb_table.subscriptions.ttl[0].enabled
    )
    error_message = "Realtime fanout must use expiring exact-topic subscriptions with indexed connection cleanup."
  }

  assert {
    condition = (
      aws_dynamodb_table.changefeed_idempotency.hash_key == "event_id" &&
      aws_dynamodb_table.changefeed_idempotency.billing_mode == "PAY_PER_REQUEST" &&
      aws_dynamodb_table.changefeed_idempotency.ttl[0].enabled &&
      aws_dynamodb_table.changefeed_idempotency.ttl[0].attribute_name == "expires_at" &&
      aws_dynamodb_table.changefeed_idempotency.server_side_encryption[0].enabled &&
      aws_lambda_function.changefeed.environment[0].variables.HINDSIGHT_CHANGEFEED_IDEMPOTENCY_TABLE == aws_dynamodb_table.changefeed_idempotency.name &&
      aws_lambda_function.changefeed.timeout == 30 &&
      tonumber(aws_lambda_function.changefeed.environment[0].variables.HINDSIGHT_CHANGEFEED_LEASE_SECONDS) == 60 &&
      aws_lambda_function.changefeed.timeout < tonumber(aws_lambda_function.changefeed.environment[0].variables.HINDSIGHT_CHANGEFEED_LEASE_SECONDS)
    )
    error_message = "Changefeed delivery must use encrypted bounded event-id leases that outlive one invocation."
  }

  assert {
    condition = (
      aws_apigatewayv2_route.product_v2_root.route_key == "ANY /v2" &&
      aws_apigatewayv2_route.product_v2_root.authorization_type == "JWT" &&
      aws_apigatewayv2_route.product_v2_proxy.route_key == "ANY /v2/{proxy+}" &&
      aws_apigatewayv2_route.product_v2_proxy.authorization_type == "JWT" &&
      aws_lambda_function.api.environment[0].variables.HINDSIGHT_REQUIRE_TENANT_CONTEXT == "1" &&
      aws_lambda_function.worker.environment[0].variables.HINDSIGHT_REQUIRE_TENANT_CONTEXT == "1"
    )
    error_message = "The product API must require the Cognito authorizer before reaching tenant-bound runtime paths."
  }

  assert {
    condition = (
      aws_apigatewayv2_authorizer.product.authorizer_type == "JWT" &&
      toset(aws_apigatewayv2_authorizer.product.identity_sources) == toset(["$request.header.Authorization"]) &&
      aws_apigatewayv2_route.public_v1_root_get.route_key == "GET /v1" &&
      aws_apigatewayv2_route.public_v1_proxy_get.route_key == "GET /v1/{proxy+}" &&
      aws_apigatewayv2_route.public_v1_ticket_post.route_key == "POST /v1/realtime/ticket" &&
      aws_apigatewayv2_route.public_v1_root_options.authorization_type == "NONE" &&
      aws_apigatewayv2_route.public_v1_proxy_options.authorization_type == "NONE" &&
      aws_apigatewayv2_route.product_v2_root_options.authorization_type == "NONE" &&
      aws_apigatewayv2_route.product_v2_proxy_options.authorization_type == "NONE"
    )
    error_message = "Public v1 routes and unauthenticated preflight routes must remain explicit around the JWT-protected v2 boundary."
  }

  assert {
    condition = (
      aws_cognito_user_pool.product.user_pool_tier == "LITE" &&
      aws_cognito_user_pool.product.admin_create_user_config[0].allow_admin_create_user_only &&
      aws_cognito_user_pool.product.username_configuration[0].case_sensitive == false &&
      one(aws_cognito_user_pool.product.account_recovery_setting[0].recovery_mechanism).name == "admin_only" &&
      aws_cognito_user_pool.product.password_policy[0].minimum_length == 14 &&
      aws_cognito_user_pool_client.product.generate_secret == false &&
      toset(aws_cognito_user_pool_client.product.allowed_oauth_flows) == toset(["code"]) &&
      toset(aws_cognito_user_pool_client.product.allowed_oauth_scopes) == toset(["openid"]) &&
      aws_cognito_user_pool_client.product.access_token_validity == 15 &&
      contains(aws_cognito_user_pool_client.product.explicit_auth_flows, "ALLOW_ADMIN_USER_PASSWORD_AUTH") &&
      !contains(aws_cognito_user_pool_client.product.explicit_auth_flows, "ALLOW_USER_PASSWORD_AUTH") &&
      !contains(aws_cognito_user_pool_client.product.explicit_auth_flows, "ALLOW_USER_SRP_AUTH") &&
      !contains(aws_cognito_user_pool_client.product.explicit_auth_flows, "ALLOW_REFRESH_TOKEN_AUTH") &&
      aws_cognito_user_group.viewer.name == "viewer" &&
      aws_cognito_user_group.operator.name == "operator"
    )
    error_message = "Cognito must be a low-cost, admin-provisioned PKCE identity boundary without a browser password grant."
  }

  assert {
    condition = (
      aws_dynamodb_table.realtime_tickets.hash_key == "ticket_digest" &&
      aws_dynamodb_table.realtime_tickets.billing_mode == "PAY_PER_REQUEST" &&
      aws_dynamodb_table.realtime_tickets.ttl[0].enabled &&
      aws_dynamodb_table.realtime_tickets.ttl[0].attribute_name == "expires_at" &&
      aws_dynamodb_table.realtime_tickets.server_side_encryption[0].enabled &&
      anytrue([
        for index in aws_dynamodb_table.realtime_tickets.global_secondary_index :
        index.hash_key == "tenant_id" && index.name == "tenant-id-index"
      ]) &&
      aws_lambda_function.api.environment[0].variables.HINDSIGHT_REALTIME_TICKET_TABLE == aws_dynamodb_table.realtime_tickets.name &&
      aws_lambda_function.websocket.environment[0].variables.HINDSIGHT_REALTIME_TICKET_TABLE == aws_dynamodb_table.realtime_tickets.name
    )
    error_message = "Realtime tickets must use encrypted expiring digest records with a tenant cleanup index."
  }

  assert {
    condition = (
      length([
        for statement in data.aws_iam_policy_document.api.statement : statement
        if statement.sid == "RealtimeTicketIssue"
        ]) == 1 && toset(one([
          for statement in data.aws_iam_policy_document.api.statement : statement
          if statement.sid == "RealtimeTicketIssue"
      ]).actions) == toset(["dynamodb:PutItem"]) &&
      length([
        for statement in data.aws_iam_policy_document.websocket.statement : statement
        if statement.sid == "RealtimeTicketRedeem"
        ]) == 1 && toset(one([
          for statement in data.aws_iam_policy_document.websocket.statement : statement
          if statement.sid == "RealtimeTicketRedeem"
      ]).actions) == toset(["dynamodb:DeleteItem"])
    )
    error_message = "The API may only issue ticket rows and the WebSocket runtime may only atomically redeem them."
  }

  assert {
    condition = (
      length(aws_wafv2_web_acl.ui) == 0 &&
      aws_cloudfront_distribution.ui.web_acl_id == null
    )
    error_message = "The default deployment must incur no Web ACL resources or attachment."
  }

  assert {
    condition     = aws_dynamodb_table.gemini_key_health.ttl[0].enabled
    error_message = "Gemini cooldown records must expire."
  }

  assert {
    condition     = local.operation_poll_seconds == 2400
    error_message = "The deployed UI must wait through the complete production retry budget."
  }

  assert {
    condition     = cloudflare_dns_record.ui[0].proxied == false
    error_message = "CloudFront must use a DNS-only Cloudflare alias."
  }

  assert {
    condition     = length(aws_cloudwatch_metric_alarm.lambda_errors) == 4
    error_message = "Every Lambda surface must have an error alarm."
  }

  assert {
    condition = (
      aws_iam_role.apigateway_cloudwatch.name == "hindsight-demo-apigateway-cloudwatch" &&
      aws_iam_role_policy_attachment.apigateway_cloudwatch.role == aws_iam_role.apigateway_cloudwatch.name &&
      endswith(aws_iam_role_policy_attachment.apigateway_cloudwatch.policy_arn, "AmazonAPIGatewayPushToCloudWatchLogs")
    )
    error_message = "API Gateway access logs require the Terraform-owned account role and managed policy."
  }
}

run "waf_enabled" {
  command = plan

  variables {
    enable_waf        = true
    api_zip_path      = "../../../src/hindsight/web/favicon.svg"
    worker_zip_path   = "../../../src/hindsight/web/favicon.svg"
    realtime_zip_path = "../../../src/hindsight/web/favicon.svg"
  }

  assert {
    condition = (
      length(aws_wafv2_web_acl.ui) == 1 &&
      aws_wafv2_web_acl.ui[0].scope == "CLOUDFRONT" &&
      length(aws_wafv2_web_acl.ui[0].rule) == 2 &&
      anytrue([
        for rule in aws_wafv2_web_acl.ui[0].rule :
        rule.name == "ip-rate-limit"
      ]) &&
      anytrue([
        for rule in aws_wafv2_web_acl.ui[0].rule :
        rule.name == "aws-managed-common"
      ])
    )
    error_message = "Opting into WAF must attach CloudFront rate limiting and the AWS managed common protections."
  }
}

run "validation_timing_profile" {
  command = plan

  variables {
    validation_mode   = true
    api_zip_path      = "../../../src/hindsight/web/favicon.svg"
    worker_zip_path   = "../../../src/hindsight/web/favicon.svg"
    realtime_zip_path = "../../../src/hindsight/web/favicon.svg"
  }

  assert {
    condition = (
      aws_lambda_function.worker.timeout == 30 &&
      tonumber(aws_lambda_function.worker.environment[0].variables.HINDSIGHT_RUN_ATTEMPT_LEASE_SECONDS) == 60 &&
      aws_sqs_queue.runs.visibility_timeout_seconds == 180 &&
      aws_sqs_queue.run_dlq.visibility_timeout_seconds == 180 &&
      tonumber(aws_lambda_function.worker.environment[0].variables.HINDSIGHT_RUN_MAX_ATTEMPTS) == 3
    )
    error_message = "Validation mode must shorten timing without weakening the attempt budget."
  }

  assert {
    condition = (
      aws_sqs_queue.runs.visibility_timeout_seconds >= aws_lambda_function.worker.timeout * 6 &&
      aws_cloudwatch_event_rule.run_dispatcher.schedule_expression == "rate(1 minute)" &&
      aws_cloudwatch_event_rule.operation_reaper.schedule_expression == "rate(1 minute)" &&
      toset(aws_lambda_event_source_mapping.worker.function_response_types) == toset(["ReportBatchItemFailures"]) &&
      toset(aws_lambda_event_source_mapping.worker_dlq.function_response_types) == toset(["ReportBatchItemFailures"])
    )
    error_message = "Validation mode must preserve scheduler and queue execution boundaries."
  }

  assert {
    condition     = local.operation_poll_seconds == 450
    error_message = "The deployed UI must wait through the complete validation retry budget."
  }
}

run "inactive_candidate_plane" {
  command = plan

  variables {
    stage               = "candidate"
    runtime_active      = false
    manage_public_dns   = false
    domain_name         = "candidate.hindsight.example.com"
    public_origin       = "https://candidate.hindsight.example.com"
    cloudfront_aliases  = ["candidate.hindsight.example.com", "*.hindsight.example.com"]
    acm_certificate_arn = "arn:aws:acm:us-east-1:123456789012:certificate/test"
    api_zip_path        = "../../../src/hindsight/web/favicon.svg"
    worker_zip_path     = "../../../src/hindsight/web/favicon.svg"
    realtime_zip_path   = "../../../src/hindsight/web/favicon.svg"
  }

  assert {
    condition = (
      aws_lambda_event_source_mapping.worker.enabled == false &&
      aws_lambda_event_source_mapping.worker_dlq.enabled == false &&
      aws_cloudwatch_event_rule.run_dispatcher.state == "DISABLED" &&
      aws_cloudwatch_event_rule.operation_reaper.state == "DISABLED"
    )
    error_message = "An inactive candidate must not consume queued or scheduled work."
  }

  assert {
    condition     = length(cloudflare_dns_record.ui) == 0
    error_message = "A candidate without DNS ownership must not create or replace the public CNAME."
  }

  assert {
    condition = (
      toset(aws_cloudfront_distribution.ui.aliases) == toset(var.cloudfront_aliases) &&
      aws_lambda_function.api.environment[0].variables.HINDSIGHT_ALLOWED_ORIGINS == var.public_origin
    )
    error_message = "Candidate aliases and browser origin must remain independently configurable."
  }
}
