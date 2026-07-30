# infra/modules/stack — the full per-environment stack (KMS, ECR, the four
# data buckets, and the per-component IAM roles). Composed by the thin roots
# in infra/envs/<env>/main.tf; contains no environment-specific literals
# other than what is passed in via variables.

data "aws_caller_identity" "current" {}

locals {
  name_prefix = "${var.project}-" # e.g. "intake-"
  account_id  = data.aws_caller_identity.current.account_id

  # Deterministic ARNs computed by naming convention rather than by
  # referencing the actual resources, specifically to avoid a dependency
  # cycle between the KMS key policy (which must name the roles allowed to
  # use the key) and each role's own permission policy (which must name the
  # key it is allowed to use). Both sides are still enforced: the KMS module
  # grants these exact ARNs in its key policy, and the iam_role modules below
  # attach a permission policy referencing the *real* module.kms.key_arn.
  training_role_arn        = "arn:aws:iam::${local.account_id}:role/${local.name_prefix}training-${var.environment}"
  endpoint_role_arn        = "arn:aws:iam::${local.account_id}:role/${local.name_prefix}endpoint-${var.environment}"
  ci_deploy_role_arn       = "arn:aws:iam::${local.account_id}:role/${local.name_prefix}ci-deploy-${var.environment}"
  github_oidc_provider_arn = "arn:aws:iam::${local.account_id}:oidc-provider/token.actions.githubusercontent.com"

  # Must match infra/bootstrap's state bucket naming exactly.
  state_bucket_name = "intake-tfstate-${local.account_id}"
  state_bucket_arn  = "arn:aws:s3:::${local.state_bucket_name}"

  common_tags = {
    environment = var.environment
  }
}

# ---------------------------------------------------------------------------
# KMS — one key per environment. See docs/decisions.md for why one key per
# environment (not per bucket, not one shared key across environments).
# ---------------------------------------------------------------------------
module "kms" {
  source = "../kms"

  alias_name  = "alias/${local.name_prefix}${var.environment}"
  description = "Intake platform data key (${var.environment}) — encrypts raw/processed/artifacts/data-capture buckets and SageMaker training/endpoint I/O."

  # Empty by default: key administration then flows through the key policy's
  # root statement plus IAM, which is the AWS-recommended pattern and avoids
  # baking a caller-dependent principal into the policy. Set
  # `kms_key_admin_principal_arns` in the env tfvars to a durable IAM role or
  # user ARN if you want an explicit break-glass administrator.
  key_admin_role_arns = var.kms_key_admin_principal_arns
  # ci-deploy needs GenerateDataKey/Decrypt too: pushing/pulling images to a
  # KMS-encrypted ECR repository requires the calling principal to have key
  # access, not just the ECR API permissions (AWS ECR + KMS requirement).
  key_user_role_arns = [local.training_role_arn, local.endpoint_role_arn, local.ci_deploy_role_arn]

  tags = merge(local.common_tags, { component = "kms" })
}

# ---------------------------------------------------------------------------
# ECR — one repository for the inference/training container image.
# ---------------------------------------------------------------------------
module "ecr" {
  source = "../ecr"

  name        = "${local.name_prefix}inference-${var.environment}"
  kms_key_arn = module.kms.key_arn

  tags = merge(local.common_tags, { component = "inference" })
}
