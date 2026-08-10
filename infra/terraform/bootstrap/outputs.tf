output "state_bucket" {
  value = data.aws_s3_bucket.state.id
}

output "github_deploy_role_arn" {
  value = aws_iam_role.github_deploy.arn
}

output "github_observability_evidence_role_arn" {
  value = aws_iam_role.github_observability_evidence.arn
}

output "github_evidence_role_arn" {
  value = try(aws_iam_role.github_evidence[0].arn, null)
}

output "github_lifecycle_role_arn" {
  value = aws_iam_role.github_lifecycle.arn
}

output "tenant_lifecycle_export_bucket" {
  value = aws_s3_bucket.tenant_lifecycle_exports.bucket
}

output "tenant_lifecycle_export_bucket_arn" {
  value = local.lifecycle_export_bucket_arn
}

output "cold_region_lifecycle_export_bucket" {
  value = try(aws_s3_bucket.tenant_lifecycle_recovery[0].bucket, null)
}

output "cold_region_lifecycle_export_bucket_arn" {
  value = var.enable_cold_region_recovery_profile ? local.lifecycle_recovery_bucket_arn : null
}

output "cold_region_recovery_profile" {
  value = var.enable_cold_region_recovery_profile ? {
    enabled                    = true
    primary_region             = var.aws_region
    recovery_region            = var.cold_region_recovery_region
    lifecycle_export_bucket    = aws_s3_bucket.tenant_lifecycle_exports.bucket
    recovery_export_bucket     = aws_s3_bucket.tenant_lifecycle_recovery[0].bucket
    recovery_export_bucket_arn = local.lifecycle_recovery_bucket_arn
    replication_role_arn       = local.lifecycle_replication_role_arn
    provisioning               = "cross-region-replication"
  } : null
}

output "learning_evidence_bucket" {
  value = try(aws_s3_bucket.learning_evidence[0].id, null)
}

output "learning_corpus_kms_key_arn" {
  value = try(aws_kms_key.learning_corpus[0].arn, null)
}

output "learning_corpus_kms_key_alias" {
  value = try(aws_kms_alias.learning_corpus[0].name, null)
}

output "learning_qualification_hmac_key_arn" {
  value = try(aws_kms_key.learning_qualification_hmac[0].arn, null)
}

output "learning_qualification_hmac_key_alias" {
  value = try(aws_kms_alias.learning_qualification_hmac[0].name, null)
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

output "edge_backend_config" {
  value = {
    bucket       = data.aws_s3_bucket.state.id
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
