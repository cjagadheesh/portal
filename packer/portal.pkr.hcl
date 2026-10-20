# Bakes the slow part of a portal boot (apt packages, uv, docker) into an AMI, so `portal up`
# boots in a fraction of the time. Everything per-box (home volume, user, agent, rsync) still
# happens at boot from user data, so one image serves every box and every config.
#
#   packer init packer/
#   packer build -var region=... -var arch=arm64 -var instance_type=... \
#     -var subnet_id=... -var security_group_id=... -var instance_profile=... packer/
#
# No SSH ingress anywhere (hard rule 1): Packer reaches the build instance through SSM
# Session Manager, using the control plane's egress-only security group and instance profile.
# Needs the session-manager-plugin on the machine running packer.

packer {
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = ">= 1.3.0"
    }
  }
}

variable "region" {
  type        = string
  description = "Region to build in. Must be the region your control plane lives in."
}

variable "arch" {
  type        = string
  description = "CPU architecture to build for: arm64 or amd64 (must match your instance types)."
  validation {
    condition     = contains(["arm64", "amd64"], var.arch)
    error_message = "The arch must be arm64 or amd64."
  }
}

variable "instance_type" {
  type        = string
  description = "Instance type to build on (a small one of the same architecture is fine)."
}

variable "subnet_id" {
  type        = string
  description = "A public subnet in the control plane's VPC (the build needs outbound internet)."
}

variable "security_group_id" {
  type        = string
  description = "The control plane's egress-only security group (`portal init` output)."
}

variable "instance_profile" {
  type        = string
  description = "The control plane's instance profile, which allows SSM (`portal init` output)."
}

variable "ami_name_prefix" {
  type        = string
  default     = "portal-ubuntu-2404-"
  description = "Must equal ami_name_prefix in your portal config so `portal up` finds the image."
}

variable "owner" {
  type        = string
  default     = "packer"
  description = "Value for the portal:owner tag."
}

locals {
  built_at = timestamp()
  # If a build hangs, the reaper cleans it up: the build instance is tagged like a box that
  # expires in an hour.
  expires = formatdate("YYYY-MM-DD'T'hh:mm:ss'Z'", timeadd(local.built_at, "1h"))
  image_tags = {
    "portal:managed" = "true"
    "portal:owner"   = var.owner
    "portal:role"    = "ami"
  }
}

# Ubuntu 24.04's current AMI id for this architecture, from Canonical's public SSM
# parameter: no AMI id and no account id appears in this repository.
data "amazon-parameterstore" "base" {
  name   = "/aws/service/canonical/ubuntu/server/24.04/stable/current/${var.arch}/hvm/ebs-gp3/ami-id"
  region = var.region
}

source "amazon-ebs" "portal" {
  region        = var.region
  source_ami    = data.amazon-parameterstore.base.value
  instance_type = var.instance_type
  ami_name      = "${var.ami_name_prefix}${var.arch}-${formatdate("YYYYMMDDhhmmss", local.built_at)}"

  subnet_id                   = var.subnet_id
  security_group_id           = var.security_group_id
  iam_instance_profile        = var.instance_profile
  associate_public_ip_address = true

  communicator  = "ssh"
  ssh_username  = "ubuntu"
  ssh_interface = "session_manager"

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  run_tags = merge(local.image_tags, {
    "portal:box"        = "ami-build"
    "portal:expires-at" = local.expires
  })
  tags          = local.image_tags
  snapshot_tags = local.image_tags
}

build {
  sources = ["source.amazon-ebs.portal"]

  # Generated from portal/bootstrap.py (`python -m portal.bootstrap`); a test keeps it in sync.
  provisioner "shell" {
    script          = "${path.root}/provision.sh"
    execute_command = "sudo -E bash '{{ .Path }}'"
  }
}
