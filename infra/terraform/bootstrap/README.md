# Terraform bootstrap

Bootstrap is intentionally separate from routine application lifecycle. Run it once with trusted local AWS credentials to create the versioned state bucket and GitHub OIDC deployment role. Its state bucket has `prevent_destroy` and is never targeted by the application destroy workflow.

```bash
terraform -chdir=infra/terraform/bootstrap init
terraform -chdir=infra/terraform/bootstrap apply
```

Record the `state_bucket` and `github_deploy_role_arn` outputs as GitHub repository variables. If the AWS account already has a GitHub OIDC provider, set `create_github_oidc_provider = false` and pass its ARN instead of attempting to create a duplicate provider.

The deploy and destroy jobs target the GitHub `demo` environment and run only from `main`. Add a required reviewer to that environment before the first live apply when the repository plan supports environment protection rules.
