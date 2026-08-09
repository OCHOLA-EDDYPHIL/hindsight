output "ui_url" {
  value = local.public_origin != "" ? local.public_origin : "https://${aws_cloudfront_distribution.ui.domain_name}"
}

output "cloudfront_distribution_domain_name" {
  value = aws_cloudfront_distribution.ui.domain_name
}

output "cloudfront_aliases" {
  value = local.cloudfront_aliases
}

output "runtime_active" {
  value = var.runtime_active
}

output "api_url" {
  value = aws_apigatewayv2_api.http.api_endpoint
}

output "public_api_url" {
  value = "${aws_apigatewayv2_api.http.api_endpoint}/v1"
}

output "product_api_url" {
  value = "${aws_apigatewayv2_api.http.api_endpoint}/v2"
}

output "cognito_user_pool_id" {
  value = aws_cognito_user_pool.product.id
}

output "cognito_user_pool_client_id" {
  value = aws_cognito_user_pool_client.product.id
}

output "cognito_issuer" {
  value = local.cognito_issuer
}

output "cognito_hosted_ui_base_url" {
  value = local.cognito_hosted_ui_base_url
}

output "cognito_authorize_endpoint" {
  value = "${local.cognito_hosted_ui_base_url}/oauth2/authorize"
}

output "cognito_logout_endpoint" {
  value = "${local.cognito_hosted_ui_base_url}/logout"
}

output "websocket_url" {
  value = "${replace(aws_apigatewayv2_api.websocket.api_endpoint, "https://", "wss://")}/${aws_apigatewayv2_stage.websocket.name}"
}

output "changefeed_webhook_url" {
  value = "${aws_apigatewayv2_api.http.api_endpoint}/internal/changefeed"
}

output "run_queue_url" {
  value = aws_sqs_queue.runs.url
}

output "worker_timeout_seconds" {
  value = local.worker_timeout_seconds
}

output "run_attempt_lease_seconds" {
  value = local.run_attempt_lease_seconds
}

output "run_queue_visibility_seconds" {
  value = local.run_queue_visibility_seconds
}

output "run_max_attempts" {
  value = local.run_max_attempts
}

output "run_dispatch_schedule_seconds" {
  value = local.run_dispatch_schedule_seconds
}

output "artifact_bucket" {
  value = aws_s3_bucket.artifacts.id
}

output "ui_bucket" {
  value = aws_s3_bucket.ui.id
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.ui.id
}

output "gemini_key_health_table" {
  value = aws_dynamodb_table.gemini_key_health.name
}

output "realtime_ticket_table" {
  value = aws_dynamodb_table.realtime_tickets.name
}

output "waf_web_acl_arn" {
  value = var.enable_waf ? aws_wafv2_web_acl.ui[0].arn : null
}
