# Terraform bootstrap

Bootstrap is intentionally separate from routine application lifecycle. It reuses an existing versioned state bucket and GitHub OIDC provider, then creates the Hindsight deployment role, ACM certificate, and Cloudflare validation records. These resources are never targeted by the application destroy workflow.

```bash
export AWS_PROFILE=your-profile
export CLOUDFLARE_API_TOKEN=your-scoped-token
terraform -chdir=infra/terraform/bootstrap init \
  -backend-config="bucket=your-existing-state-bucket" \
  -backend-config="key=hindsight/bootstrap/terraform.tfstate" \
  -backend-config="region=us-east-1" \
  -backend-config="encrypt=true" \
  -backend-config="use_lockfile=true"
terraform -chdir=infra/terraform/bootstrap apply -var-file=terraform.tfvars
```

Record the state bucket, deployment role, ACM certificate, domain, and Cloudflare zone outputs as GitHub repository variables. Set `create_github_oidc_provider = false` and pass the existing provider ARN when the AWS account already trusts GitHub.
