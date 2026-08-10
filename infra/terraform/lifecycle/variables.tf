variable "aws_region" {
  type    = string
  default = "us-east-1"
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
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.stage))
    error_message = "stage must be a lowercase Hindsight environment name."
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
