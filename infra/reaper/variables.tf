variable "region" {
  description = "Region the reaper runs in; scopes the terminate permission to instances there."
  type        = string
}

variable "name_prefix" {
  description = "Prefix for resource names."
  type        = string
  default     = "portal"
}

variable "dry_run" {
  description = "When true the reaper only logs what it would terminate. Use for the first deploy."
  type        = bool
  default     = false
}

variable "invalid_tag_grace_minutes" {
  description = "How long a managed box with a missing or unreadable portal:expires-at may live (counted from launch) before the reaper terminates it."
  type        = number
  default     = 60
}

variable "log_retention_days" {
  description = "How long the reaper's decisions stay in CloudWatch Logs."
  type        = number
  default     = 14
}
