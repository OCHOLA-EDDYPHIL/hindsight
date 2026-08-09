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

variable "lifecycle_database_url_parameter_name" {
  description = "Dedicated SecureString parameter for the non-bypass-RLS lifecycle database login."
  type        = string
  default     = "/hindsight/demo/lifecycle-database-url"

  validation {
    condition     = can(regex("^/hindsight/[a-z][a-z0-9-]{1,15}/lifecycle-database-url$", var.lifecycle_database_url_parameter_name))
    error_message = "lifecycle_database_url_parameter_name must be a stage-scoped Hindsight lifecycle parameter path."
  }
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

variable "edge_state_key" {
  description = "Remote-state object that owns the stable public DNS record independently of either app plane."
  type        = string
  default     = "hindsight/edge/terraform.tfstate"
}

variable "enable_learning_infrastructure" {
  description = "Retain the source account's immutable learning archive and evidence role. Disable only for a fresh product-only account."
  type        = bool
  default     = true
}

variable "enable_cold_region_recovery_profile" {
  description = "Provision the opt-in locked cross-region lifecycle export replica and replication path."
  type        = bool
  default     = false
}

variable "cold_region_recovery_region" {
  description = "AWS region operators target when the cold-region recovery drill profile is enabled."
  type        = string
  default     = "us-west-2"

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.cold_region_recovery_region))
    error_message = "cold_region_recovery_region must be an AWS region identifier."
  }
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
