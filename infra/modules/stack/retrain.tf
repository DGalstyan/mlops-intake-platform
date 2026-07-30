# infra/modules/stack/retrain.tf — the M5 loop, deployed on demand.
#
# Gated behind `deploy_retrain` for the same reason as the intake module: it needs a
# built Lambda package. It also depends on the intake stack existing, because the
# drift job reads the endpoint's data capture and the promote state machine updates
# the endpoint the endpoint module creates.

module "retrain" {
  count  = var.deploy_retrain ? 1 : 0
  source = "../retrain"

  name_prefix = local.name_prefix
  environment = var.environment
  region      = var.region
  account_id  = local.account_id
  kms_key_arn = module.kms.key_arn

  artifacts_bucket_name    = module.artifacts_bucket.bucket_id
  artifacts_bucket_arn     = module.artifacts_bucket.bucket_arn
  data_capture_bucket_name = module.data_capture_bucket.bucket_id
  data_capture_bucket_arn  = module.data_capture_bucket.bucket_arn

  training_role_arn  = module.training_role.role_arn
  training_role_name = module.training_role.role_name
  endpoint_role_arn  = module.endpoint_role.role_arn

  model_package_group_name = aws_sagemaker_model_package_group.classifier.model_package_group_name
  alarm_topic_arn          = module.observability.alarm_topic_arn
  lambda_package_path      = var.lambda_package_path

  # Empty when no endpoint is deployed, which disables the promote path entirely —
  # there is nothing to promote to, and a promotion state machine pointing at a
  # non-existent endpoint would fail at its last state after doing real work.
  endpoint_name      = var.deploy_endpoint ? "${local.name_prefix}classifier-${var.environment}" : ""
  five_xx_alarm_name = var.deploy_endpoint ? "${local.name_prefix}endpoint-5xx-${var.environment}" : ""
  latency_alarm_name = var.deploy_endpoint ? "${local.name_prefix}endpoint-latency-${var.environment}" : ""

  inference_image_uri = "${module.ecr.repository_url}:${var.image_tag}"

  numpy_layer_arn        = var.numpy_layer_arn
  endpoint_instance_type = var.endpoint_instance_type
  git_sha                = var.git_sha

  enable_xray        = var.enable_xray
  log_retention_days = var.log_retention_days

  tags = local.common_tags
}
