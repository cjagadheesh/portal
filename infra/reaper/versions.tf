terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0, < 7.0"
    }
    # Zips handler.py at plan time so there is no separate build step to forget.
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }
}
