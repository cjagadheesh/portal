variable "region" {
  description = "AWS region for the control plane. Comes from portal config; never hardcoded."
  type        = string
}

variable "owner" {
  description = "OS username of whoever runs `portal init`; written to the portal:owner tag."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Monthly spend limit for the whole account, in USD (AWS Budgets bills in USD). No default on purpose: you pick your own ceiling."
  type        = number

  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "monthly_budget_usd must be greater than 0."
  }
}

variable "alert_email" {
  description = "Optional email subscribed to budget alerts. AWS sends a confirmation link you must click. Null means the SNS topic has no subscribers."
  type        = string
  default     = null
}

variable "vpc_id" {
  description = "VPC for the security group. Null uses the region's default VPC."
  type        = string
  default     = null
}

variable "name_prefix" {
  description = "Prefix for resource names."
  type        = string
  default     = "portal"
}

variable "reaper_dry_run" {
  description = "Deploy the reaper in log-only mode (it decides but terminates nothing). Useful for the first deploy."
  type        = bool
  default     = false
}
