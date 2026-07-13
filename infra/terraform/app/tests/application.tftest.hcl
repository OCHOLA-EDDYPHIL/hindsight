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
}
