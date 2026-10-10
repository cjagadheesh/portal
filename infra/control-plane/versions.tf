terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0, < 7.0"
    }
  }
}

provider "aws" {
  region = var.region

  # Rule 2: tags set once here land on every taggable resource below, so a resource
  # added later cannot be forgotten. Control-plane resources live as long as the
  # account setup, so they get no portal:box / portal:expires-at (those are per-box).
  default_tags {
    tags = {
      "portal:managed" = "true"
      "portal:owner"   = var.owner
    }
  }
}
