# infra/bootstrap — owns the Terraform remote-state backend itself.
#
# This root is applied ONCE, by hand, via `make bootstrap`, with LOCAL state
# (it cannot store its own state in the S3 bucket it creates). Every other
# root (infra/envs/dev, infra/envs/staging) uses the S3 bucket created here
# as its remote backend. Do not add application resources here.

terraform {
  required_version = ">= 1.10.0" # S3 native state locking (use_lockfile) needs >= 1.10

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
}
