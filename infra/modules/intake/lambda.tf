# infra/modules/intake/lambda.tf — the three Lambda functions and their roles.
#
# ONE deployment package, three functions. The handlers import nothing outside the
# standard library and boto3 (which the runtime provides), so there is no dependency
# layer and no build container — `make package-lambdas` stages four Python files.
# Three separate zips of identical content would be three things to keep in step for
# no benefit.
#
# Each function still gets its OWN role, scoped to only what it touches. Sharing one
# role across all three would give the OCR normaliser the review API's ability to
# resume executions, which is the capability that matters most in this system.

locals {
  lambda_prefix = "${var.name_prefix}${var.environment}"

  # Recomputed from the built artifact, so a code change redeploys the function.
  # Without this Terraform sees no diff when only the zip's contents changed.
  package_hash = filebase64sha256(var.lambda_package_path)
}

# ---------------------------------------------------------------------------
# Shared trust policy for Lambda execution roles.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume" {
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

# Log-write permissions, scoped to each function's own log group rather than to all
# of /aws/lambda/*. Written as a template so each role gets its own instance.
data "aws_iam_policy_document" "normalize_ocr_permissions" {
  statement {
    sid    = "OwnLogGroup"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.normalize_ocr.arn}:*"]
  }

  # X-Ray segment submission has no resource-level permissions in AWS's IAM model.
  # Listed explicitly rather than folded into a broader statement so the exception
  # is visible; see the wildcard inventory in docs/decisions.md.
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

data "aws_iam_policy_document" "validate_permissions" {
  statement {
    sid    = "OwnLogGroup"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.validate.arn}:*"]
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

data "aws_iam_policy_document" "review_api_permissions" {
  statement {
    sid    = "OwnLogGroup"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.review_api.arn}:*"]
  }

  statement {
    sid    = "ReadAndCloseReviewTasks"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
    ]
    resources = [
      aws_dynamodb_table.review_queue.arn,
      "${aws_dynamodb_table.review_queue.arn}/index/*",
    ]
  }

  statement {
    # The capability that matters. Scoped to this one state machine: a token for any
    # other state machine in the account cannot be resumed by this role.
    #
    # NOTE: SendTaskSuccess is authorised against the STATE MACHINE, not the
    # execution, because a task token does not carry an execution ARN that IAM can
    # match. That is why the application-level control — looking the token up from
    # DynamoDB rather than accepting it from the caller — is load-bearing and not
    # merely defence in depth.
    sid    = "ResumeIntakeExecutions"
    effect = "Allow"
    actions = [
      "states:SendTaskSuccess",
      "states:SendTaskFailure",
      "states:SendTaskHeartbeat",
    ]
    resources = [aws_sfn_state_machine.intake.arn]
  }

  statement {
    sid    = "UseProjectKey"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]
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

# ---------------------------------------------------------------------------
# Log groups, created explicitly rather than letting Lambda create them.
#
# A Lambda-created log group has no retention (logs accumulate forever, billed) and
# no KMS key. Creating them here also means `make destroy` removes them — an
# implicitly-created group survives the function and is exactly the kind of orphan
# the "destroy leaves nothing behind" criterion is about.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "normalize_ocr" {
  name              = "/aws/lambda/${local.lambda_prefix}-normalize-ocr"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = merge(var.tags, { Name = "${local.lambda_prefix}-normalize-ocr-logs" })
}

resource "aws_cloudwatch_log_group" "validate" {
  name              = "/aws/lambda/${local.lambda_prefix}-validate"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = merge(var.tags, { Name = "${local.lambda_prefix}-validate-logs" })
}

resource "aws_cloudwatch_log_group" "review_api" {
  name              = "/aws/lambda/${local.lambda_prefix}-review-api"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = merge(var.tags, { Name = "${local.lambda_prefix}-review-api-logs" })
}

# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
module "normalize_ocr_role" {
  source = "../iam_role"

  role_name               = "${local.lambda_prefix}-normalize-ocr"
  description             = "Lambda: assembles reading-order text from Textract blocks (component=normalize-ocr)."
  assume_role_policy_json = data.aws_iam_policy_document.lambda_assume.json
  inline_policy_json      = data.aws_iam_policy_document.normalize_ocr_permissions.json

  tags = merge(var.tags, { component = "normalize-ocr" })
}

module "validate_role" {
  source = "../iam_role"

  role_name               = "${local.lambda_prefix}-validate"
  description             = "Lambda: JSON Schema and cross-field validation of extracted fields (component=validate)."
  assume_role_policy_json = data.aws_iam_policy_document.lambda_assume.json
  inline_policy_json      = data.aws_iam_policy_document.validate_permissions.json

  tags = merge(var.tags, { component = "validate" })
}

module "review_api_role" {
  source = "../iam_role"

  role_name               = "${local.lambda_prefix}-review-api"
  description             = "Lambda: reviewer-facing correction API; resumes waiting executions (component=review-api)."
  assume_role_policy_json = data.aws_iam_policy_document.lambda_assume.json
  inline_policy_json      = data.aws_iam_policy_document.review_api_permissions.json

  tags = merge(var.tags, { component = "review-api" })
}

# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------
resource "aws_lambda_function" "normalize_ocr" {
  function_name = "${local.lambda_prefix}-normalize-ocr"
  description   = "Textract block graph -> reading-order text + content hash."
  role          = module.normalize_ocr_role.role_arn

  filename         = var.lambda_package_path
  source_code_hash = local.package_hash
  handler          = "src.pipeline.handlers.normalize_ocr_handler"
  runtime          = var.lambda_runtime
  memory_size      = var.lambda_memory_mb
  timeout          = var.lambda_timeout_seconds

  tracing_config {
    mode = var.enable_xray ? "Active" : "PassThrough"
  }

  # Log group must exist first, or Lambda creates an unmanaged one with no
  # retention and no KMS key on first invocation.
  depends_on = [aws_cloudwatch_log_group.normalize_ocr]

  tags = merge(var.tags, { Name = "${local.lambda_prefix}-normalize-ocr" })
}

resource "aws_lambda_function" "validate" {
  function_name = "${local.lambda_prefix}-validate"
  description   = "JSON Schema + cross-field validation of Bedrock-extracted fields."
  role          = module.validate_role.role_arn

  filename         = var.lambda_package_path
  source_code_hash = local.package_hash
  handler          = "src.pipeline.handlers.validate_handler"
  runtime          = var.lambda_runtime
  memory_size      = var.lambda_memory_mb
  timeout          = var.lambda_timeout_seconds

  tracing_config {
    mode = var.enable_xray ? "Active" : "PassThrough"
  }

  depends_on = [aws_cloudwatch_log_group.validate]

  tags = merge(var.tags, { Name = "${local.lambda_prefix}-validate" })
}

resource "aws_lambda_function" "review_api" {
  function_name = "${local.lambda_prefix}-review-api"
  description   = "Reviewer correction API. Resumes a waiting execution via SendTaskSuccess."
  role          = module.review_api_role.role_arn

  filename         = var.lambda_package_path
  source_code_hash = local.package_hash
  handler          = "src.pipeline.review_api.lambda_handler"
  runtime          = var.lambda_runtime
  memory_size      = var.lambda_memory_mb
  timeout          = var.lambda_timeout_seconds

  environment {
    variables = {
      REVIEW_QUEUE_TABLE = aws_dynamodb_table.review_queue.name
    }
  }

  tracing_config {
    mode = var.enable_xray ? "Active" : "PassThrough"
  }

  depends_on = [aws_cloudwatch_log_group.review_api]

  tags = merge(var.tags, { Name = "${local.lambda_prefix}-review-api" })
}
