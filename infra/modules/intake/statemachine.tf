# infra/modules/intake/statemachine.tf — the intake state machine, its permission
# policy, and the EventBridge rule that starts it.

locals {
  state_machine_name = "${var.name_prefix}intake-${var.environment}"

  # Endpoint ARN for scoping InvokeEndpoint. When no endpoint is deployed this
  # resolves to a name that does not exist, so the permission is still narrow and an
  # execution fails at Classify with an accurate AccessDenied/NotFound rather than
  # the state machine failing to deploy. Granting "*" to cover the not-yet-deployed
  # case would be the wrong trade.
  endpoint_name = var.endpoint_name != "" ? var.endpoint_name : "${var.name_prefix}classifier-${var.environment}"
  endpoint_arn  = "arn:aws:sagemaker:${var.region}:${var.account_id}:endpoint/${local.endpoint_name}"
}

# ---------------------------------------------------------------------------
# Definition. Placeholders in statemachines/intake.asl.json are substituted here,
# so the ASL file itself contains no account id, no region and no ARN — it stays
# readable, diffable, and testable by tests/test_asl.py without Terraform.
# ---------------------------------------------------------------------------
locals {
  intake_definition = templatefile(
    "${path.module}/../../../statemachines/intake.asl.json",
    {
      LedgerTable             = aws_dynamodb_table.ledger.name
      ResultsTable            = aws_dynamodb_table.results.name
      ReviewQueueTable        = aws_dynamodb_table.review_queue.name
      CorrectionsTable        = aws_dynamodb_table.corrections.name
      PromptsTable            = aws_dynamodb_table.prompts.name
      EndpointName            = local.endpoint_name
      BedrockModelId          = var.bedrock_model_id
      DeadLetterQueueUrl      = aws_sqs_queue.dead_letter.url
      NormalizeOcrFunctionArn = aws_lambda_function.normalize_ocr.arn
      ValidateFunctionArn     = aws_lambda_function.validate.arn
      Environment             = var.environment
    }
  )
}

# ---------------------------------------------------------------------------
# The state machine's permission policy.
#
# Attached to the role created in infra/modules/stack, which was deliberately
# trust-only at M0 because none of these target resources existed yet. This is the
# milestone that creates them, so this is where the permissions belong — every
# statement below names a real ARN.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "state_machine_permissions" {
  statement {
    sid    = "IdempotencyLedger"
    effect = "Allow"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:GetItem",
    ]
    resources = [aws_dynamodb_table.ledger.arn]
  }

  statement {
    sid       = "WriteResults"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.results.arn]
  }

  statement {
    sid       = "WriteReviewTasks"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.review_queue.arn]
  }

  statement {
    sid       = "WriteCorrections"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.corrections.arn]
  }

  statement {
    # Read-only on prompts. The state machine renders no prompts; it reads what
    # `make seed-prompts` wrote. Write access here would let a workflow bug corrupt
    # the extraction prompt for every subsequent document.
    sid       = "ReadPrompts"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.prompts.arn]
  }

  statement {
    # Textract needs no resource scoping for DetectDocumentText on an S3 object —
    # the S3 read is authorised separately below, which is the control that matters.
    sid       = "DetectDocumentText"
    effect    = "Allow"
    actions   = ["textract:DetectDocumentText"]
    resources = ["*"]
  }

  statement {
    sid       = "ReadUploadedDocuments"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${var.raw_bucket_arn}/*"]
  }

  statement {
    sid       = "InvokeClassifier"
    effect    = "Allow"
    actions   = ["sagemaker:InvokeEndpoint"]
    resources = [local.endpoint_arn]
  }

  statement {
    # Scoped to the specific foundation model, not to bedrock:* on "*". A model id
    # change is a tfvars edit that re-scopes this statement, which is the point of
    # making the model a variable.
    sid       = "InvokeExtractionModel"
    effect    = "Allow"
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:aws:bedrock:${var.region}::foundation-model/${var.bedrock_model_id}"]
  }

  statement {
    sid     = "InvokePipelineLambdas"
    effect  = "Allow"
    actions = ["lambda:InvokeFunction"]
    resources = [
      aws_lambda_function.normalize_ocr.arn,
      aws_lambda_function.validate.arn,
    ]
  }

  statement {
    sid       = "DeadLetter"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.dead_letter.arn]
  }

  statement {
    sid    = "UseProjectKey"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]
    resources = [var.kms_key_arn]
  }

  statement {
    sid    = "ExecutionLogging"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.state_machine.arn}:*"]
  }

  statement {
    # The CloudWatch Logs *delivery* API, which Step Functions uses to attach its
    # vended log group. These actions genuinely reject resource-level permissions —
    # a documented AWS restriction, not a scoping choice. This is the statement
    # deleted at M0 as premature; it is added here because now there is a state
    # machine that needs it. See the "deleted over-engineering" decision entry.
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

  dynamic "statement" {
    for_each = var.enable_xray ? [1] : []
    content {
      sid    = "XRayTracing"
      effect = "Allow"
      actions = [
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "xray:GetSamplingRules",
        "xray:GetSamplingTargets",
      ]
      resources = ["*"]
    }
  }
}

resource "aws_iam_role_policy" "state_machine" {
  name   = "${local.state_machine_name}-permissions"
  role   = var.state_machine_role_name
  policy = data.aws_iam_policy_document.state_machine_permissions.json
}

resource "aws_cloudwatch_log_group" "state_machine" {
  # Step Functions requires the /aws/vendedlogs/ prefix for execution logging.
  name              = "/aws/vendedlogs/states/${local.state_machine_name}"
  retention_in_days = var.log_retention_days
  tags              = merge(var.tags, { Name = "${local.state_machine_name}-logs" })
}

resource "aws_sfn_state_machine" "intake" {
  name     = local.state_machine_name
  role_arn = "arn:aws:iam::${var.account_id}:role/${var.state_machine_role_name}"

  # STANDARD, not EXPRESS. Express executions cannot use .waitForTaskToken and are
  # capped at five minutes — this workflow waits up to seven days for a human. The
  # per-transition cost of STANDARD is irrelevant next to Textract and Bedrock.
  type = "STANDARD"

  definition = local.intake_definition

  logging_configuration {
    log_destination = "${aws_cloudwatch_log_group.state_machine.arn}:*"
    # ALL, not ERROR: the M3 deliverable is an end-to-end trace of one document, and
    # an error-only log has nothing to show for a successful execution.
    level                  = "ALL"
    include_execution_data = true
  }

  tracing_configuration {
    enabled = var.enable_xray
  }

  depends_on = [aws_iam_role_policy.state_machine]

  tags = merge(var.tags, { Name = local.state_machine_name })
}

# ---------------------------------------------------------------------------
# EventBridge: S3 object created -> start execution.
#
# The execution NAME is derived deterministically from the object identity, which
# completes the idempotency story: Step Functions rejects a duplicate execution name
# outright, so most redeliveries never start an execution at all. The ledger claim
# inside the workflow catches the remainder (a redelivery after the dedupe window).
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# S3 -> EventBridge. WITHOUT THIS, NOTHING RUNS.
#
# S3 does not publish `Object Created` to EventBridge unless notifications are
# explicitly enabled on the bucket. The rule below matches an event that would never
# be delivered: `terraform apply` succeeds, the state machine exists, the rule exists,
# and not one document is ever processed.
#
# Nothing catches this without a deployment — the rule is syntactically valid, the
# IAM is correct, and every test passes. It was found by an audit reading the
# resource graph for what was ABSENT rather than checking what was present.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket_notification" "raw_to_eventbridge" {
  bucket      = var.raw_bucket_name
  eventbridge = true
}

resource "aws_cloudwatch_event_rule" "document_uploaded" {
  name        = "${local.state_machine_name}-uploaded"
  description = "A document landed in the raw bucket; start intake."

  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created"]
    detail = {
      bucket = {
        name = [var.raw_bucket_name]
      }
      object = {
        # Only the incoming/ prefix. Without this, anything else written to the
        # bucket — including a future export or a manifest — would start an
        # execution and dead-letter as an unreadable document.
        key = [{ prefix = "incoming/" }]
      }
    }
  })

  tags = merge(var.tags, { Name = "${local.state_machine_name}-uploaded" })
}

data "aws_iam_policy_document" "eventbridge_assume" {
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
      values   = [aws_cloudwatch_event_rule.document_uploaded.arn]
    }
  }
}

data "aws_iam_policy_document" "eventbridge_permissions" {
  statement {
    sid       = "StartIntakeExecution"
    effect    = "Allow"
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.intake.arn]
  }
}

module "eventbridge_role" {
  source = "../iam_role"

  role_name               = "${local.state_machine_name}-events"
  description             = "EventBridge role that starts intake executions on S3 upload (component=eventbridge)."
  assume_role_policy_json = data.aws_iam_policy_document.eventbridge_assume.json
  inline_policy_json      = data.aws_iam_policy_document.eventbridge_permissions.json

  tags = merge(var.tags, { component = "eventbridge" })
}

resource "aws_cloudwatch_event_target" "start_intake" {
  rule     = aws_cloudwatch_event_rule.document_uploaded.name
  arn      = aws_sfn_state_machine.intake.arn
  role_arn = module.eventbridge_role.role_arn

  # Reshape the S3 event into the workflow's input contract. Done here rather than
  # in a Lambda: an input transformer is configuration, and a Lambda whose only job
  # is to rename three fields is the definition of glue.
  input_transformer {
    input_paths = {
      bucket    = "$.detail.bucket.name"
      key       = "$.detail.object.key"
      versionId = "$.detail.object.version-id"
    }
    input_template = <<-JSON
      {
        "document": {
          "bucket": <bucket>,
          "key": <key>,
          "version_id": <versionId>
        }
      }
    JSON
  }

  # Failures to *start* an execution go to the same dead-letter queue as failures
  # inside one. Without this, a throttled StartExecution silently drops the
  # document before any of the workflow's own retry logic can see it — a gap that is
  # easy to miss because every retry policy in the ASL is downstream of it.
  dead_letter_config {
    arn = aws_sqs_queue.dead_letter.arn
  }

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 4
  }
}

# EventBridge must be allowed to write to the DLQ it is configured to use.
data "aws_iam_policy_document" "dlq_policy" {
  statement {
    sid    = "AllowEventBridgeDeadLetter"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.dead_letter.arn]
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.document_uploaded.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "dead_letter" {
  queue_url = aws_sqs_queue.dead_letter.id
  policy    = data.aws_iam_policy_document.dlq_policy.json
}
