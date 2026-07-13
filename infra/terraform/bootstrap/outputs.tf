output "state_bucket" {
  value = data.aws_s3_bucket.state.id
}

output "github_deploy_role_arn" {
  value = aws_iam_role.github_deploy.arn
}

output "backend_config" {
  value = {
    bucket       = data.aws_s3_bucket.state.id
    key          = var.application_state_key
    region       = var.aws_region
    use_lockfile = true
    encrypt      = true
  }
}

output "acm_certificate_arn" {
  value = aws_acm_certificate_validation.demo.certificate_arn
}

output "domain_name" {
  value = var.domain_name
}

output "cloudflare_zone_id" {
  value = var.cloudflare_zone_id
}
