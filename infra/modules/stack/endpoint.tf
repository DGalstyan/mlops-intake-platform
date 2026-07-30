# infra/modules/stack/endpoint.tf — the inference endpoint, deployed on demand.
#
# Gated behind `deploy_endpoint` (default false) rather than always created, for
# two reasons:
#
#   1. It cannot be created until an Approved model package version exists, and
#      that requires a training run plus a human approval. An unconditional module
#      would make `terraform plan` fail on a clean account with a validation error
#      about a missing ARN, breaking M0's "plan builds the whole stack" deliverable
#      for a reason unrelated to M0.
#   2. It is the only resource in this stack with a standing hourly cost. Making
#      its creation an explicit opt-in means `make apply` cannot start billing for
#      an endpoint by accident, which matters against a ~$15 budget.
#
# Turn it on with `deploy_endpoint = true` and a resolved
# `model_package_arn` — see scripts/resolve_approved_model.py, which refuses to
# return anything that is not Approved.

module "endpoint" {
  count  = var.deploy_endpoint ? 1 : 0
  source = "../endpoint"

  name_prefix = local.name_prefix
  environment = var.environment
  region      = var.region
  account_id  = local.account_id

  execution_role_arn       = module.endpoint_role.role_arn
  kms_key_arn              = module.kms.key_arn
  data_capture_bucket_name = module.data_capture_bucket.bucket_id

  model_package_arn = var.model_package_arn

  instance_type                    = var.endpoint_instance_type
  initial_instance_count           = var.endpoint_initial_instance_count
  data_capture_sampling_percentage = var.endpoint_data_capture_sampling_percentage

  autoscaling_max_instances                   = var.endpoint_autoscaling_max_instances
  autoscaling_target_invocations_per_instance = var.endpoint_autoscaling_target_invocations

  canary_traffic_percentage     = var.endpoint_canary_traffic_percentage
  canary_bake_time_minutes      = var.endpoint_canary_bake_time_minutes
  rollback_5xx_threshold        = var.endpoint_rollback_5xx_threshold
  rollback_latency_threshold_ms = var.endpoint_rollback_latency_threshold_ms

  # The rollback alarms drive the canary rollback whether or not anyone is
  # subscribed, but they also publish to the M4 topic so a rollback is announced
  # rather than discovered later in the deployment history.
  alarm_sns_topic_arns = concat(
    [module.observability.alarm_topic_arn],
    var.alarm_sns_topic_arns,
  )

  tags = merge(local.common_tags, { component = "endpoint" })
}
