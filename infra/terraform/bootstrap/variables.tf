variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "state_bucket_name" {
  description = "Optional globally unique state bucket name."
  type        = string
  default     = null
  nullable    = true
}

variable "github_repository" {
  description = "GitHub owner/repository allowed to assume the deployment role."
  type        = string
  default     = "OCHOLA-EDDYPHIL/hindsight"
}

variable "github_subjects" {
  description = "Allowed GitHub OIDC subject patterns. Narrow these to protected environments when configured."
  type        = list(string)
  default     = ["repo:OCHOLA-EDDYPHIL/hindsight:*"]
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
