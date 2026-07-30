# infra/modules/stack/observability.tf — dashboard, alarms and the alarm topic.
#
# Always created, unlike the endpoint and intake modules. Two reasons: a dashboard and
# a handful of alarms cost a few dollars a month rather than an hourly rate, and an
# observability stack that only exists once the thing it watches is deployed is
# useless during the deploy — which is exactly when a rollback alarm has to already
# be there.
#
# NOTE ON A MODULE CYCLE THAT IS AVOIDED HERE. The endpoint module needs the SNS topic
# ARN for its rollback alarms, and the observability module needs the endpoint's and
# state machine's NAMES for its AWS/SageMaker and AWS/States widgets. Passing module
# outputs both ways would be a dependency cycle Terraform rejects.
#
# Resolved by passing observability the *deterministic* names rather than the module
# outputs. Those names are already a naming contract in this repo (the same reason the
# state bucket name is derived rather than read), so this introduces no new coupling —
# and the alarms are created before the resources they watch, which is the correct
# order for a rollback alarm.

module "observability" {
  source = "../observability"

  name_prefix = local.name_prefix
  environment = var.environment
  region      = var.region

  prices_file = "${path.module}/../../../config/prices.json"

  # Deterministic names, not module outputs — see the note above. Empty string when
  # the component is not deployed, which the observability module treats as "omit
  # that widget and that alarm" rather than rendering a panel for a resource that
  # does not exist.
  state_machine_name      = var.deploy_intake ? "${local.name_prefix}intake-${var.environment}" : ""
  endpoint_name           = var.deploy_endpoint ? "${local.name_prefix}classifier-${var.environment}" : ""
  dead_letter_queue_name  = var.deploy_intake ? "${local.name_prefix}${var.environment}-dlq" : ""
  review_queue_table_name = var.deploy_intake ? "${local.name_prefix}${var.environment}-review-queue" : ""

  alarm_email = var.alarm_email

  tags = merge(local.common_tags, { component = "observability" })
}
