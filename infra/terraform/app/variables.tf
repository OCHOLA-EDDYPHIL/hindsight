variable "aws_region" {
  description = "AWS region for the application runtime."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Resource-name prefix."
  type        = string
  default     = "hindsight"
}

variable "stage" {
  description = "Deployment environment name."
  type        = string
  default     = "demo"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.stage))
    error_message = "stage must be 2-16 lowercase letters, numbers, or hyphens."
  }
}

variable "validation_mode" {
  description = "Use bounded timing for owner-authorized hosted product acceptance."
  type        = bool
  default     = false
}

variable "api_database_url_parameter_name" {
  description = "Existing SecureString parameter containing the restricted API CockroachDB URL."
  type        = string
  default     = "/hindsight/demo/api-database-url"
}

variable "worker_database_url_parameter_name" {
  description = "Existing SecureString parameter containing the restricted worker CockroachDB URL."
  type        = string
  default     = "/hindsight/demo/worker-database-url"
}

variable "gemini_api_keys_parameter_name" {
  description = "Existing SecureString parameter containing the versioned Gemini key pool."
  type        = string
  default     = "/hindsight/demo/gemini-api-keys"
}

variable "changefeed_token_parameter_name" {
  description = "Existing SecureString parameter containing the CockroachDB webhook token."
  type        = string
  default     = "/hindsight/demo/changefeed-token"
}

variable "llm_provider" {
  description = "Live reasoning provider."
  type        = string
  default     = "gemini"

  validation {
    condition     = var.llm_provider == "gemini"
    error_message = "hosted llm_provider must be gemini."
  }
}

variable "gemini_model" {
  type    = string
  default = "gemini-3.1-flash-lite"
}

variable "embedding_provider" {
  type    = string
  default = "gemini"

  validation {
    condition     = var.embedding_provider == "gemini"
    error_message = "hosted embedding_provider must be gemini."
  }
}

variable "gemini_embedding_model" {
  type    = string
  default = "gemini-embedding-2"
}

variable "gemini_embedding_representation" {
  description = "Candidate-neutral Gemini retrieval representation."
  type        = string
  default     = "raw_control"

  validation {
    condition = contains([
      "raw_control",
      "generic_title",
      "applicability_instruction",
    ], var.gemini_embedding_representation)
    error_message = "Gemini embedding representation is not supported."
  }
}

variable "reasoning_max_attempts" {
  type    = number
  default = 2
}

variable "api_zip_path" {
  description = "Optional override for the HTTP API zip."
  type        = string
  default     = null
  nullable    = true
}

variable "worker_zip_path" {
  description = "Optional override for the LangGraph worker zip."
  type        = string
  default     = null
  nullable    = true
}

variable "realtime_zip_path" {
  description = "Optional override for the WebSocket/changefeed zip."
  type        = string
  default     = null
  nullable    = true
}

variable "domain_name" {
  description = "Optional CloudFront custom domain."
  type        = string
  default     = null
  nullable    = true
}

variable "public_origin" {
  description = "Browser origin authorized by the API. Defaults to domain_name when present."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.public_origin == null || can(regex("^https://[A-Za-z0-9.-]+$", var.public_origin))
    error_message = "public_origin must be an HTTPS origin without a path."
  }
}

variable "cloudfront_aliases" {
  description = "Custom aliases attached to the distribution. Defaults to domain_name when present."
  type        = list(string)
  default     = null
  nullable    = true

  validation {
    condition = var.cloudfront_aliases == null || alltrue([
      for alias in var.cloudfront_aliases :
      can(regex("^(\\*\\.)?[A-Za-z0-9.-]+$", alias))
    ])
    error_message = "cloudfront_aliases must contain only DNS names or leftmost wildcards."
  }
}

variable "manage_public_dns" {
  description = "Allow this application state to own the domain_name CNAME."
  type        = bool
  default     = false
}

variable "runtime_active" {
  description = "Enable schedules, queue consumers, and external changefeed configuration."
  type        = bool
  default     = true
}

variable "acm_certificate_arn" {
  description = "us-east-1 ACM certificate for domain_name."
  type        = string
  default     = null
  nullable    = true
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone for the custom-domain CNAME."
  type        = string
  default     = null
  nullable    = true
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "deployed_revision" {
  description = "Exact source revision exposed by product health responses."
  type        = string
  default     = "unknown"
}

variable "alarm_actions" {
  description = "Optional SNS topic ARNs for operational alarms."
  type        = list(string)
  default     = []
}

variable "alert_email" {
  description = "Optional confirmed subscriber for operational and budget notifications."
  type        = string
  default     = null
  nullable    = true
}

variable "enable_bounded_observability" {
  description = "Enable the opt-in ADOT/X-Ray and custom-metric profile."
  type        = bool
  default     = false
}

variable "adot_python_layer_arn" {
  description = "Region-matched AWS Distro for OpenTelemetry Python Lambda layer ARN."
  type        = string
  default     = null
  nullable    = true
}

variable "enable_waf" {
  description = "Create and attach the CloudFront Web ACL."
  type        = bool
  default     = false
}
