# infra/envs/staging — thin root: configure the provider and instantiate the
# stack module. No resources, no business logic here (see
# infra/modules/stack for that) — this file should stay tiny even as new
# modules are added in later milestones. Structurally identical to
# infra/envs/dev/main.tf; only the tfvars differ.

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project     = var.project
      environment = var.environment
      managed_by  = "terraform"
    }
  }
}

module "stack" {
  source = "../../modules/stack"

  environment       = var.environment
  region            = var.region
  project           = var.project
  github_repository = var.github_repository

  kms_key_admin_principal_arns = var.kms_key_admin_principal_arns

  deploy_endpoint        = var.deploy_endpoint
  model_package_arn      = var.model_package_arn
  endpoint_instance_type = var.endpoint_instance_type

  raw_expiration_days               = var.raw_expiration_days
  processed_transition_days         = var.processed_transition_days
  artifacts_transition_ia_days      = var.artifacts_transition_ia_days
  artifacts_transition_glacier_days = var.artifacts_transition_glacier_days
  data_capture_expiration_days      = var.data_capture_expiration_days
}
