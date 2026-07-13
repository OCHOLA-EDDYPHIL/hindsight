output "ui_url" {
  value = var.domain_name != null ? "https://${var.domain_name}" : "https://${aws_cloudfront_distribution.ui.domain_name}"
}

output "api_url" {
  value = aws_apigatewayv2_api.http.api_endpoint
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
