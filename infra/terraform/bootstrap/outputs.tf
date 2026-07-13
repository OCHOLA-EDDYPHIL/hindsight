output "state_bucket" {
  value = aws_s3_bucket.state.id
}

output "github_deploy_role_arn" {
  value = aws_iam_role.github_deploy.arn
}

output "backend_config" {
  value = {
    bucket       = aws_s3_bucket.state.id
    key          = "hindsight/demo/terraform.tfstate"
    region       = var.aws_region
    use_lockfile = true
    encrypt      = true
  }
}
