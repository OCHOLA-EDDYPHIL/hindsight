output "state_bucket" {
  value = var.state_bucket_name
}

output "github_deploy_role_arn" {
  value = aws_iam_role.github_deploy.arn
}

output "github_oidc_provider_arn" {
  value = local.oidc_arn
}

output "github_observability_evidence_role_arn" {
  value = aws_iam_role.github_observability_evidence.arn
}

output "github_quarantine_redrive_role_arn" {
  value = aws_iam_role.github_quarantine_redrive.arn
}

output "github_worker_acceptance_role_arn" {
  value = aws_iam_role.github_worker_acceptance.arn
}

output "github_evidence_role_arn" {
  value = try(aws_iam_role.github_evidence[0].arn, null)
}

output "learning_evidence_bucket" {
  value = try(aws_s3_bucket.learning_evidence[0].id, null)
}

output "learning_qualification_hmac_key_arn" {
  value = try(aws_kms_key.learning_qualification_hmac[0].arn, null)
}

output "learning_qualification_hmac_key_alias" {
  value = try(aws_kms_alias.learning_qualification_hmac[0].name, null)
}

output "backend_config" {
  value = {
    bucket       = var.state_bucket_name
    key          = var.application_state_key
    region       = var.aws_region
    use_lockfile = true
    encrypt      = true
  }
}

output "edge_backend_config" {
  value = {
    bucket       = var.state_bucket_name
    key          = var.edge_state_key
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
