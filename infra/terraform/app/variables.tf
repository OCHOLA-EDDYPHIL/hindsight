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

variable "database_url_parameter_name" {
  description = "Existing SecureString parameter containing the CockroachDB URL."
  type        = string
  default     = "/hindsight/demo/database-url"
}

variable "gemini_api_keys_parameter_name" {
  description = "Existing SecureString parameter containing the versioned Gemini key pool."
  type        = string
  default     = "/hindsight/demo/gemini-api-keys"
}

variable "operator_token_parameter_name" {
  description = "Existing SecureString parameter containing the operator token."
  type        = string
  default     = "/hindsight/demo/operator-token"
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
    condition     = contains(["gemini", "bedrock", "deterministic"], var.llm_provider)
    error_message = "llm_provider must be gemini, bedrock, or deterministic."
  }
}

variable "gemini_model" {
  type    = string
  default = "gemini-2.5-flash"
}

variable "embedding_provider" {
  type    = string
  default = "gemini"

  validation {
    condition     = contains(["gemini", "bedrock"], var.embedding_provider)
    error_message = "hosted embedding_provider must be gemini or bedrock."
  }
}

variable "gemini_embedding_model" {
  type    = string
  default = "gemini-embedding-2"
}

variable "bedrock_model" {
  type    = string
  default = "anthropic.claude-3-haiku-20240307-v1:0"
}

variable "bedrock_embedding_model" {
  type    = string
  default = "amazon.titan-embed-text-v2:0"
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

variable "alarm_actions" {
  description = "Optional SNS topic ARNs for operational alarms."
  type        = list(string)
  default     = []
}
