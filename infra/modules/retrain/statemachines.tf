# infra/modules/retrain/statemachines.tf — the retrain and promote state machines,
# and the EventBridge rule that connects a human's approval to a deployment.

locals {
  training_image = var.training_image_uri != "" ? var.training_image_uri : var.inference_image_uri

  retrain_definition = templatefile(
    "${path.module}/../../../statemachines/retrain.asl.json",
    {
      TrainingJobPrefix      = "${local.prefix}-retrain"
      EvaluationJobPrefix    = "${local.prefix}-evaluate"
      TrainingImageUri       = local.training_image
      InferenceImageUri      = var.inference_image_uri
      TrainingRoleArn        = var.training_role_arn
      TrainingInstanceType   = var.training_instance_type
      EvaluationInstanceType = var.evaluation_instance_type
      ArtifactsBucket        = var.artifacts_bucket_name
      ArtifactsS3Prefix      = "s3://${var.artifacts_bucket_name}"
      KmsKeyArn              = var.kms_key_arn
      ModelPackageGroupName  = var.model_package_group_name
      AlarmTopicArn          = var.alarm_topic_arn
      GitSha                 = var.git_sha
    }
  )

  promote_definition = templatefile(
    "${path.module}/../../../statemachines/promote.asl.json",
    {
      ModelNamePrefix               = "${var.name_prefix}promoted-${var.environment}"
      EndpointConfigNamePrefix      = "${var.name_prefix}promoted-${var.environment}"
      EndpointRoleArn               = var.endpoint_role_arn
      EndpointName                  = var.endpoint_name
      KmsKeyArn                     = var.kms_key_arn
      InstanceType                  = var.endpoint_instance_type
      InitialInstanceCount          = var.endpoint_instance_count
      DataCaptureSamplingPercentage = var.data_capture_sampling_percentage
      DataCaptureS3Uri              = local.data_capture_s3_prefix
      CanaryTrafficPercentage       = var.canary_traffic_percentage
      CanaryBakeTimeSeconds         = var.canary_bake_time_minutes * 60
      CanaryBakeTimeMinutes         = var.canary_bake_time_minutes
      FiveXxAlarmName               = var.five_xx_alarm_name
      LatencyAlarmName              = var.latency_alarm_name
      AlarmTopicArn                 = var.alarm_topic_arn
    }
  )
}

# ===========================================================================
# Retrain state machine
# ===========================================================================

resource "aws_cloudwatch_log_group" "retrain" {
  name              = "/aws/vendedlogs/states/${local.retrain_sm_name}"
  retention_in_days = var.log_retention_days
  tags              = merge(var.tags, { Name = "${local.retrain_sm_name}-logs" })
}

data "aws_iam_policy_document" "sfn_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

data "aws_iam_policy_document" "retrain_permissions" {
  statement {
    sid    = "RunTrainingAndEvaluation"
    effect = "Allow"
    actions = [
      "sagemaker:CreateTrainingJob",
      "sagemaker:DescribeTrainingJob",
      "sagemaker:StopTrainingJob",
      "sagemaker:CreateProcessingJob",
      "sagemaker:DescribeProcessingJob",
      "sagemaker:StopProcessingJob",
      "sagemaker:AddTags",
    ]
    resources = [
      "arn:aws:sagemaker:${var.region}:${var.account_id}:training-job/${local.prefix}-retrain*",
      "arn:aws:sagemaker:${var.region}:${var.account_id}:processing-job/${local.prefix}-evaluate*",
    ]
  }

  statement {
    # .sync integrations poll via EventBridge managed rules. Without this the state
    # machine starts the job and then hangs — the job succeeds and the workflow never
    # learns, which looks like a stuck execution rather than a permissions problem.
    sid    = "SyncIntegrationCallbacks"
    effect = "Allow"
    actions = [
      "events:PutTargets",
      "events:PutRule",
      "events:DescribeRule",
    ]
    resources = [
      "arn:aws:events:${var.region}:${var.account_id}:rule/StepFunctionsGetEventsForSageMakerTrainingJobsRule",
      "arn:aws:events:${var.region}:${var.account_id}:rule/StepFunctionsGetEventsForSageMakerProcessingJobsRule",
    ]
  }

  statement {
    sid       = "PassTrainingRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [var.training_role_arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["sagemaker.amazonaws.com"]
    }
  }

  statement {
    # Register only. No UpdateEndpoint, no CreateEndpointConfig — the retrain state
    # machine has no path to production and this policy is what enforces that beyond
    # the definition's own shape.
    sid       = "RegisterCandidate"
    effect    = "Allow"
    actions   = ["sagemaker:CreateModelPackage"]
    resources = ["arn:aws:sagemaker:${var.region}:${var.account_id}:model-package/${var.model_package_group_name}/*"]
  }

  statement {
    sid       = "ReadEvaluationOutput"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [var.artifacts_bucket_arn, "${var.artifacts_bucket_arn}/*"]
  }

  statement {
    sid       = "Notify"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [var.alarm_topic_arn]
  }

  statement {
    sid       = "UseProjectKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [var.kms_key_arn]
  }

  statement {
    sid       = "ExecutionLogging"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.retrain.arn}:*"]
  }

  statement {
    # The CloudWatch Logs delivery API rejects resource-level permissions — see the
    # wildcard inventory in docs/decisions.md.
    sid    = "LogDelivery"
    effect = "Allow"
    actions = [
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }
}

module "retrain_role" {
  source = "../iam_role"

  role_name               = "${local.retrain_sm_name}-execution"
  description             = "Retrain state machine (component=retrain). Can train, evaluate and REGISTER — deliberately cannot deploy."
  assume_role_policy_json = data.aws_iam_policy_document.sfn_assume.json
  inline_policy_json      = data.aws_iam_policy_document.retrain_permissions.json

  tags = merge(var.tags, { component = "retrain" })
}

resource "aws_sfn_state_machine" "retrain" {
  name       = local.retrain_sm_name
  role_arn   = module.retrain_role.role_arn
  type       = "STANDARD"
  definition = local.retrain_definition

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.retrain.arn}:*"
    level                  = "ALL"
    include_execution_data = true
  }

  tracing_configuration {
    enabled = var.enable_xray
  }

  tags = merge(var.tags, { Name = local.retrain_sm_name })
}

# ===========================================================================
# Promote state machine — the ONLY automated path to production, and it is
# reachable only from a human's approval.
# ===========================================================================

resource "aws_cloudwatch_log_group" "promote" {
  count             = local.has_endpoint ? 1 : 0
  name              = "/aws/vendedlogs/states/${local.promote_sm_name}"
  retention_in_days = var.log_retention_days
  tags              = merge(var.tags, { Name = "${local.promote_sm_name}-logs" })
}

data "aws_iam_policy_document" "promote_permissions" {
  count = local.has_endpoint ? 1 : 0

  statement {
    sid    = "DeployApprovedModel"
    effect = "Allow"
    actions = [
      "sagemaker:CreateModel",
      "sagemaker:CreateEndpointConfig",
      "sagemaker:UpdateEndpoint",
      "sagemaker:DescribeEndpoint",
      "sagemaker:DescribeModelPackage",
      "sagemaker:AddTags",
    ]
    resources = [
      "arn:aws:sagemaker:${var.region}:${var.account_id}:model/${var.name_prefix}promoted-${var.environment}*",
      "arn:aws:sagemaker:${var.region}:${var.account_id}:endpoint-config/${var.name_prefix}promoted-${var.environment}*",
      "arn:aws:sagemaker:${var.region}:${var.account_id}:endpoint/${var.endpoint_name}",
      "arn:aws:sagemaker:${var.region}:${var.account_id}:model-package/${var.model_package_group_name}/*",
    ]
  }

  statement {
    sid       = "PassEndpointRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [var.endpoint_role_arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["sagemaker.amazonaws.com"]
    }
  }

  statement {
    sid       = "Notify"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [var.alarm_topic_arn]
  }

  statement {
    sid       = "UseProjectKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey", "kms:CreateGrant"]
    resources = [var.kms_key_arn]
    condition {
      test     = "Bool"
      variable = "kms:GrantIsForAWSResource"
      values   = ["true"]
    }
  }

  statement {
    sid       = "ExecutionLogging"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.promote[0].arn}:*"]
  }

  statement {
    sid    = "LogDelivery"
    effect = "Allow"
    actions = [
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }
}

module "promote_role" {
  count  = local.has_endpoint ? 1 : 0
  source = "../iam_role"

  role_name               = "${local.promote_sm_name}-execution"
  description             = "Promote state machine (component=promote). Deploys an APPROVED model as a canary. Startable only by the registry-approval EventBridge rule."
  assume_role_policy_json = data.aws_iam_policy_document.sfn_assume.json
  inline_policy_json      = data.aws_iam_policy_document.promote_permissions[0].json

  tags = merge(var.tags, { component = "promote" })
}

resource "aws_sfn_state_machine" "promote" {
  count = local.has_endpoint ? 1 : 0

  name       = local.promote_sm_name
  role_arn   = module.promote_role[0].role_arn
  type       = "STANDARD"
  definition = local.promote_definition

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.promote[0].arn}:*"
    level                  = "ALL"
    include_execution_data = true
  }

  tags = merge(var.tags, { Name = local.promote_sm_name })
}

# ---------------------------------------------------------------------------
# The approval event.
#
# This rule is the entire mechanism behind "a human approves and the canary starts".
# It fires on a Model Package State Change where the status became Approved — an event
# SageMaker emits when a person clicks approve in the Model Registry, or when someone
# calls UpdateModelPackage deliberately. The retrain state machine cannot produce it,
# because it only ever writes PendingManualApproval.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "model_approved" {
  count = local.has_endpoint ? 1 : 0

  name        = "${local.prefix}-model-approved"
  description = "A human approved a model package version — start the canary deploy."

  event_pattern = jsonencode({
    source        = ["aws.sagemaker"]
    "detail-type" = ["SageMaker Model Package State Change"]
    detail = {
      ModelPackageGroupName = [var.model_package_group_name]
      ModelApprovalStatus   = ["Approved"]
    }
  })

  tags = merge(var.tags, { Name = "${local.prefix}-model-approved" })
}

data "aws_iam_policy_document" "approval_events_assume" {
  count = local.has_endpoint ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.model_approved[0].arn]
    }
  }
}

data "aws_iam_policy_document" "approval_events_permissions" {
  count = local.has_endpoint ? 1 : 0

  statement {
    sid       = "StartPromotion"
    effect    = "Allow"
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.promote[0].arn]
  }
}

module "approval_events_role" {
  count  = local.has_endpoint ? 1 : 0
  source = "../iam_role"

  role_name               = "${local.prefix}-model-approved-events"
  description             = "EventBridge role that starts the promote state machine on registry approval (component=approval-events)."
  assume_role_policy_json = data.aws_iam_policy_document.approval_events_assume[0].json
  inline_policy_json      = data.aws_iam_policy_document.approval_events_permissions[0].json

  tags = merge(var.tags, { component = "approval-events" })
}

resource "aws_cloudwatch_event_target" "start_promotion" {
  count = local.has_endpoint ? 1 : 0

  rule     = aws_cloudwatch_event_rule.model_approved[0].name
  arn      = aws_sfn_state_machine.promote[0].arn
  role_arn = module.approval_events_role[0].role_arn

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 3
  }
}
