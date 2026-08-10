variable "aws_region" {
  type    = string
  default = "us-east-1"

  validation {
    condition     = var.aws_region == "us-east-1"
    error_message = "The demo lifecycle root is fixed to us-east-1."
  }
}

variable "expected_aws_account_id" {
  description = "AWS account that may own the lifecycle state and resources."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_aws_account_id))
    error_message = "expected_aws_account_id must contain exactly 12 digits."
  }
}

variable "stage" {
  type    = string
  default = "demo"

  validation {
    condition     = var.stage == "demo"
    error_message = "The lifecycle root owns only the demo environment."
  }
}

variable "github_oidc_provider_arn" {
  description = "Existing GitHub Actions OIDC provider retained by the bootstrap root."
  type        = string

  validation {
    condition     = can(regex("^arn:[a-z0-9-]+:iam::[0-9]{12}:oidc-provider/token\\.actions\\.githubusercontent\\.com$", var.github_oidc_provider_arn))
    error_message = "github_oidc_provider_arn must identify the GitHub Actions provider in one AWS account."
  }
}

variable "github_deploy_role_arn" {
  description = "Existing application deployment role denied lifecycle archive mutation."
  type        = string

  validation {
    condition     = can(regex("^arn:[a-z0-9-]+:iam::[0-9]{12}:role/hindsight-github-deploy$", var.github_deploy_role_arn))
    error_message = "github_deploy_role_arn must identify the stable Hindsight deployment role."
  }
}

variable "bootstrap_state_bucket_name" {
  description = "Existing account-scoped S3 bucket that owns the bootstrap Terraform state."
  type        = string

  validation {
    condition     = can(regex("^home-in-cloud-terraform-state-[0-9]{12}-us-east-1$", var.bootstrap_state_bucket_name))
    error_message = "bootstrap_state_bucket_name must identify the fixed us-east-1 account state bucket."
  }
}

variable "bootstrap_certificate_arn" {
  description = "Existing bootstrap-owned ACM certificate read during full bootstrap planning."
  type        = string

  validation {
    condition     = can(regex("^arn:[a-z0-9-]+:acm:us-east-1:[0-9]{12}:certificate/[0-9a-f-]{36}$", var.bootstrap_certificate_arn))
    error_message = "bootstrap_certificate_arn must identify one us-east-1 ACM certificate."
  }
}

variable "bootstrap_hmac_key_arn" {
  description = "Existing bootstrap-owned HMAC key read during full bootstrap planning."
  type        = string

  validation {
    condition     = can(regex("^arn:[a-z0-9-]+:kms:us-east-1:[0-9]{12}:key/[0-9a-f-]{36}$", var.bootstrap_hmac_key_arn))
    error_message = "bootstrap_hmac_key_arn must identify one us-east-1 KMS key."
  }
}

variable "github_subjects" {
  description = "Allowed GitHub OIDC subjects for privileged lifecycle operations."
  type        = list(string)
  default     = ["repo:OCHOLA-EDDYPHIL/hindsight:environment:demo"]

  validation {
    condition = length(var.github_subjects) > 0 && alltrue([
      for subject in var.github_subjects :
      can(regex("^repo:OCHOLA-EDDYPHIL/hindsight:environment:(demo|demo-candidate)$", subject))
    ])
    error_message = "github_subjects must name an allow-listed Hindsight lifecycle environment."
  }
}

variable "lifecycle_database_url_parameter_name" {
  description = "Dedicated SecureString parameter populated outside Terraform for the non-bypass-RLS lifecycle login."
  type        = string
  default     = "/hindsight/demo/lifecycle-database-url"

  validation {
    condition     = can(regex("^/hindsight/[a-z][a-z0-9-]{1,15}/lifecycle-database-url$", var.lifecycle_database_url_parameter_name))
    error_message = "lifecycle_database_url_parameter_name must be a stage-scoped Hindsight lifecycle parameter path."
  }
}

variable "enable_cold_region_recovery_profile" {
  description = "Provision the opt-in locked cross-region lifecycle export replica and replication path."
  type        = bool
  default     = false
}

variable "cold_region_recovery_region" {
  description = "AWS region used by the opt-in cold-region recovery profile."
  type        = string
  default     = "us-west-2"

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.cold_region_recovery_region))
    error_message = "cold_region_recovery_region must be an AWS region identifier."
  }
}
