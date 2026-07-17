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
    error_message = "The API must authorize cookie sessions from the configured CloudFront product origin."
  }

  assert {
    condition = (
      aws_lambda_function.worker.timeout == 180 &&
      tonumber(aws_lambda_function.worker.environment[0].variables.HINDSIGHT_RUN_ATTEMPT_LEASE_SECONDS) == 300 &&
      aws_sqs_queue.runs.visibility_timeout_seconds == 360 &&
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
      toset(aws_lambda_event_source_mapping.worker.function_response_types) == toset(["ReportBatchItemFailures"]) &&
      toset(aws_lambda_event_source_mapping.worker_dlq.function_response_types) == toset(["ReportBatchItemFailures"])
    )
    error_message = "Both run queues must use partial batch failure reporting."
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
    condition     = aws_dynamodb_table.gemini_key_health.ttl[0].enabled
    error_message = "Gemini cooldown records must expire."
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
}
