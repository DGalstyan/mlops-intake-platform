# infra/modules/retrain — the M5 loop: scheduled drift detection, the retrain state
# machine, and the approval-triggered canary promotion.
#
# The shape that matters is where the automation STOPS. Drift can start a retrain, and
# a retrain can register a candidate — but nothing here deploys. The promote state
# machine is reachable only from an EventBridge rule on a Model Package State Change
# with ModelApprovalStatus=Approved, which only a human clicking approve produces.
#
#   drift (scheduled)  ─┐
#                       ├─► retrain SM ─► register PendingManualApproval ─► STOP
#   manual workflow    ─┘
#
#                          [ a human approves in the Model Registry ]
#                                          │
#                                          ▼
#                          EventBridge ─► promote SM ─► canary ─► auto-rollback
#
# There is deliberately no edge from the retrain state machine to the promote one.

locals {
  prefix = "${var.name_prefix}${var.environment}"

  has_endpoint = var.endpoint_name != ""

  drift_function_name    = "${local.prefix}-drift"
  retrain_sm_name        = "${var.name_prefix}retrain-${var.environment}"
  promote_sm_name        = "${var.name_prefix}promote-${var.environment}"
  baseline_s3_uri        = "s3://${var.artifacts_bucket_name}/${var.baseline_s3_key}"
  data_capture_s3_prefix = "s3://${var.data_capture_bucket_name}/endpoint-capture/${var.environment}"

  runbook_note = "RUNBOOK: https://github.com/DGalstyan/mlops-intake-platform/blob/main/docs/runbook.md"
}

# ===========================================================================
# Drift detection — scheduled Lambda
# ===========================================================================

resource "aws_cloudwatch_log_group" "drift" {
  name              = "/aws/lambda/${local.drift_function_name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = merge(var.tags, { Name = "${local.drift_function_name}-logs" })
}

data "aws_iam_policy_document" "drift_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

data "aws_iam_policy_document" "drift_permissions" {
  statement {
    sid       = "OwnLogGroup"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.drift.arn}:*"]
  }

  statement {
    # Read-only on production traffic. The drift job compares distributions; it has no
    # reason to be able to modify or delete a capture record, and a scheduled job with
    # write access to its own evidence is a bad shape.
    sid       = "ReadDataCapture"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [var.data_capture_bucket_arn, "${var.data_capture_bucket_arn}/*"]
  }

  statement {
    sid       = "ReadBaseline"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${var.artifacts_bucket_arn}/*"]
  }

  statement {
    # Write reports only under the drift-reports/ prefix, not anywhere in the
    # artifacts bucket — the same bucket holds model.tar.gz files.
    sid       = "WriteDriftReports"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${var.artifacts_bucket_arn}/drift-reports/*"]
  }

  statement {
    sid       = "NotifyOnBreach"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [var.alarm_topic_arn]
  }

  statement {
    sid       = "UseProjectKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [var.kms_key_arn]
  }

  dynamic "statement" {
    for_each = var.enable_xray ? [1] : []
    content {
      sid       = "XRayTracing"
      effect    = "Allow"
      actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
      resources = ["*"]
    }
  }
}

module "drift_role" {
  source = "../iam_role"

  role_name               = "${local.prefix}-drift"
  description             = "Scheduled drift detection (component=drift). Reads data capture and the baseline; writes reports. No permission to start a retrain — see the module header."
  assume_role_policy_json = data.aws_iam_policy_document.drift_assume.json
  inline_policy_json      = data.aws_iam_policy_document.drift_permissions.json

  tags = merge(var.tags, { component = "drift" })
}

resource "aws_lambda_function" "drift" {
  function_name = local.drift_function_name
  description   = "Compares a production window against the M1 baseline and writes a drift report. Distinguishes 'the data changed' from 'the model got worse'."
  role          = module.drift_role.role_arn

  filename         = var.lambda_package_path
  source_code_hash = filebase64sha256(var.lambda_package_path)
  handler          = "src.drift.detect.lambda_handler"
  runtime          = "python3.12"

  # The drift math loads a whole capture window into memory. 1GB is generous for the
  # volumes this handles and is the point at which Lambda gives a full vCPU, which
  # matters because the histogram work is CPU-bound.
  memory_size = 1024
  # Under the schedule's 1/day cadence a slow run costs nothing; a truncated one loses
  # the report entirely.
  timeout = 300

  layers = var.numpy_layer_arn != "" ? [var.numpy_layer_arn] : []

  environment {
    variables = {
      BASELINE_S3_URI        = local.baseline_s3_uri
      DATA_CAPTURE_S3_PREFIX = local.data_capture_s3_prefix
      REPORT_BUCKET          = var.artifacts_bucket_name
      ALARM_TOPIC_ARN        = var.alarm_topic_arn
    }
  }

  tracing_config {
    mode = var.enable_xray ? "Active" : "PassThrough"
  }

  depends_on = [aws_cloudwatch_log_group.drift]

  tags = merge(var.tags, { Name = local.drift_function_name })
}

resource "aws_cloudwatch_event_rule" "drift_schedule" {
  name                = "${local.prefix}-drift-schedule"
  description         = "Runs drift detection against the endpoint's data capture."
  schedule_expression = var.drift_schedule_expression

  tags = merge(var.tags, { Name = "${local.prefix}-drift-schedule" })
}

resource "aws_cloudwatch_event_target" "drift" {
  rule = aws_cloudwatch_event_rule.drift_schedule.name
  arn  = aws_lambda_function.drift.arn

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 2
  }
}

resource "aws_lambda_permission" "drift_schedule" {
  statement_id  = "AllowDriftSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.drift.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.drift_schedule.arn
}

resource "aws_cloudwatch_metric_alarm" "drift_job_failing" {
  alarm_name = "${local.prefix}-drift-job-failing"
  alarm_description = join(" ", [
    "The scheduled drift job is erroring.",
    "WHAT BREAKS: nothing in the serving path — but drift stops being detected, and",
    "the failure is SILENT because a job that does not run produces no report and no",
    "breach. This alarm exists because the absence of bad news is not good news.",
    "FIRST RESPONSE: read the function's logs. A missing numpy layer and a baseline",
    "artifact that has not been uploaded are the two most likely causes, and both",
    "fail at cold start with a clear import or key error.",
    local.runbook_note,
  ])

  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.drift.function_name
  }

  alarm_actions = [var.alarm_topic_arn]
  tags          = merge(var.tags, { measures = "system health", Name = "${local.prefix}-drift-job-failing" })
}
