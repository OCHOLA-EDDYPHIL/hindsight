variable "cloudflare_zone_id" {
  description = "Cloudflare zone containing the stable product hostname."
  type        = string
}

variable "domain_name" {
  description = "Stable product hostname retained across application accounts."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9.-]+$", var.domain_name))
    error_message = "domain_name must be a DNS hostname."
  }
}

variable "target_domain_name" {
  description = "CloudFront distribution hostname currently serving the stable product hostname."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9.-]+$", var.target_domain_name))
    error_message = "target_domain_name must be a DNS hostname."
  }
}
