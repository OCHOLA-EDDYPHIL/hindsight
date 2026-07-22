variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "state_bucket_name" {
  description = "Existing versioned S3 bucket shared for Terraform state."
  type        = string
}

variable "expected_aws_account_id" {
  description = "AWS account that may own this bootstrap state and its resources."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_aws_account_id))
    error_message = "expected_aws_account_id must contain exactly 12 digits."
  }
}

variable "github_repository" {
  description = "GitHub owner/repository allowed to assume the deployment role."
  type        = string
  default     = "OCHOLA-EDDYPHIL/hindsight"
}

variable "github_subjects" {
  description = "Allowed GitHub OIDC subjects. Keep this restricted to the protected deployment environment."
  type        = list(string)
  default     = ["repo:OCHOLA-EDDYPHIL/hindsight:environment:demo"]

  validation {
    condition = length(var.github_subjects) > 0 && alltrue([
      for subject in var.github_subjects :
      can(regex("^repo:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+:environment:(demo|demo-candidate)$", subject))
    ])
    error_message = "github_subjects must name an allow-listed Hindsight deployment environment."
  }
}

variable "stage" {
  type    = string
  default = "demo"
}

variable "domain_name" {
  description = "Stable CloudFront hostname prepared for the demo."
  type        = string
  default     = "hindsight.strathmoreedu.qzz.io"
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone containing domain_name."
  type        = string
}

variable "application_state_key" {
  description = "Remote-state object used by the ephemeral application stack."
  type        = string
  default     = "hindsight/demo/terraform.tfstate"
}

variable "enable_learning_infrastructure" {
  description = "Retain the source account's immutable learning archive and evidence role. Disable only for a fresh product-only account."
  type        = bool
  default     = true
}

variable "create_github_oidc_provider" {
  description = "Set false when the AWS account already has the token.actions.githubusercontent.com provider."
  type        = bool
  default     = true
}

variable "existing_github_oidc_provider_arn" {
  type     = string
  default  = null
  nullable = true

  validation {
    condition     = var.create_github_oidc_provider || var.existing_github_oidc_provider_arn != null
    error_message = "existing_github_oidc_provider_arn is required when provider creation is disabled."
  }
}
