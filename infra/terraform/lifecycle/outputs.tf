output "github_lifecycle_role_arn" {
  value = aws_iam_role.github_lifecycle.arn
}

output "github_bootstrap_plan_role_arn" {
  value = aws_iam_role.github_bootstrap_plan.arn
}

output "lifecycle_database_url_parameter_name" {
  value = var.lifecycle_database_url_parameter_name
}

output "lifecycle_database_url_parameter_arn" {
  value = local.lifecycle_database_parameter_arn
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
