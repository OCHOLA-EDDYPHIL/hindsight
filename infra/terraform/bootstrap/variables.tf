variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "state_bucket_name" {
  description = "Existing versioned S3 bucket shared for Terraform state."
  type        = string
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

variable "bedrock_embedding_model" {
  description = "Bedrock embedding model the deployment workflow may invoke during profile rotation."
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
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
