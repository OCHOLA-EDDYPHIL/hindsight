output "state_bucket" {
  value = data.aws_s3_bucket.state.id
}

output "github_deploy_role_arn" {
  value = aws_iam_role.github_deploy.arn
}

output "github_evidence_role_arn" {
  value = aws_iam_role.github_evidence.arn
}

output "learning_evidence_bucket" {
  value = aws_s3_bucket.learning_evidence.id
}

output "learning_corpus_kms_key_arn" {
  value = aws_kms_key.learning_corpus.arn
}

output "learning_corpus_kms_key_alias" {
  value = aws_kms_alias.learning_corpus.name
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
