terraform {
  required_version = ">= 1.10.0" # S3 native state locking (use_lockfile)

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # Partial configuration: `bucket` and `region` are supplied at `terraform
  # init` time by `make init ENV=staging` (via -backend-config), which reads
  # them from infra/bootstrap's own state/output rather than hardcoding an
  # account id here. `key` is static per environment root by design — this
  # is the one place "staging" is allowed to appear literally, since it
  # names this root's own state file, not an application resource.
  backend "s3" {
    key          = "envs/staging/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}
