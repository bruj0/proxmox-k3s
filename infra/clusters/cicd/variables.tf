variable "cf_api_token" {
  type        = string
  sensitive   = true
  default     = ""
  description = "Cloudflare scoped API token."
}
variable "cf_account_id" {
  type        = string
  default     = ""
  description = "Cloudflare account ID."
}
variable "ssh_proxy_host" {
  type        = string
  default     = "10.0.0.1"
  description = "PVE host acting as the SSH jump box."
}
variable "ssh_proxy_port" {
  type        = number
  default     = 6022
  description = "PVE host's SSH port."
}
variable "ssh_proxy_user" {
  type        = string
  default     = "root"
  description = "PVE host's SSH user."
}