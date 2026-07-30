# infra/modules/stack/intake.tf — the intake orchestration, deployed on demand.
#
# Gated behind `deploy_intake` (default false) for the same two reasons the endpoint
# is: it depends on artifacts that must exist first (a built Lambda package), and
# turning it on creates billable resources. Unlike the endpoint, none of these have a
# standing hourly cost — DynamoDB is on-demand, Lambda and Step Functions are
# per-invocation — so the cost argument here is weaker and the real reason is the
# build dependency.
#
# The Lambda package is produced by `make package-lambdas`. Terraform reads it as a
# file, so a plan with `deploy_intake = true` and no built package fails with a
# missing-file error rather than deploying an empty function.

module "intake" {
  count  = var.deploy_intake ? 1 : 0
  source = "../intake"

  name_prefix = local.name_prefix
  environment = var.environment
  region      = var.region
  account_id  = local.account_id

  kms_key_arn          = module.kms.key_arn
  raw_bucket_name      = module.raw_bucket.bucket_id
  raw_bucket_arn       = module.raw_bucket.bucket_arn
  processed_bucket_arn = module.processed_bucket.bucket_arn

  # The role is created in this module (trust-only at M0) and its permission policy
  # is attached by the intake module, which is the milestone that creates the
  # resources those permissions name.
  state_machine_role_name = module.state_machine_role.role_name

  # Empty when the endpoint is not deployed. The intake module still scopes
  # InvokeEndpoint to the conventional endpoint name rather than widening to "*", so
  # an execution fails at Classify with an accurate error instead of the state machine
  # failing to deploy.
  endpoint_name = var.deploy_endpoint ? one(module.endpoint[*].endpoint_name) : ""

  bedrock_model_id    = var.bedrock_model_id
  lambda_package_path = var.lambda_package_path

  enable_xray        = var.enable_xray
  log_retention_days = var.log_retention_days

  tags = local.common_tags
}
