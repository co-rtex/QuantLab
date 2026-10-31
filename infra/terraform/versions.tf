# QuantLab on AWS (#19, ADR-0012). Validated, never applied: there is no backend and no
# credentials here. See README.md for how it was checked and how remote state would be
# configured before a first apply.

terraform {
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.71"
    }
  }

  # Remote state is documented (README.md, "Remote state"), deliberately not configured:
  # nothing may be applied until the user chooses an account and approves it.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "quantlab"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
