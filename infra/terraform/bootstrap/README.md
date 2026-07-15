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

Bedrock-related bootstrap variables and permissions are legacy wiring for the dormant adapter. The adapter is quota-deferred, unhosted, excluded from live acceptance, and not part of the submission; do not configure it for the submission deployment.

## Upgrading an existing deployment role

The application workflow cannot update its own bootstrap role. Before running live acceptance for a revision that adds AWS services or provider checks, apply the bootstrap plan with a separate trusted bootstrap administrator (or a separately protected bootstrap role). For the governed-memory release, the minimal role-policy delta is:

- the EventBridge rule/target lifecycle actions used by the operation reaper;
- the Lambda reserved-concurrency lifecycle actions used by the hosted functions;
- the finite S3 bucket/object metadata and CloudWatch log-delivery lifecycle actions required by the AWS provider.

Review the bootstrap plan and require it to update only the deployment role's inline policy when the certificate, OIDC provider, state bucket, domain, and Cloudflare records already exist. Do not place local `.env` AWS keys in the application workflow or grant the application role permission to rewrite itself. The live workflow can assume the upgraded role through the existing `repo:OCHOLA-EDDYPHIL/hindsight:environment:demo` OIDC subject after that one-time policy update.

The Cloudflare token needs only Zone DNS Edit and Zone Read for the selected zone. Store it as `CLOUDFLARE_API_TOKEN` in the GitHub `demo` environment, never in Terraform state. Routine deploy/destroy dispatches run from `main`; the owner-labelled live-acceptance workflow may deploy an exact same-repository PR SHA through the same protected environment. Add a required reviewer when repository environment protection is available.
