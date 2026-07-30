# infra/modules/intake/main.tf — the five data stores and the dead-letter queue.
#
# All five tables are PAY_PER_REQUEST. Provisioned capacity would need a throughput
# estimate this workload does not have (document arrival is bursty by nature — a
# customer uploads 50,000 documents at once, then nothing for a day), and
# under-provisioning it produces exactly the throttling the state machine then has to
# retry around. On-demand also means `make destroy` leaves no reserved capacity
# behind.

locals {
  table_prefix = "${var.name_prefix}${var.environment}"

  # Duplicated deliberately rather than passed in as a variable: this module does not
  # otherwise depend on the observability module, and adding a variable to carry a
  # constant string would create a dependency for no benefit. There is a test
  # asserting every actionable alarm carries the link, which is the property that
  # matters.
  runbook_note = "RUNBOOK: https://github.com/DGalstyan/mlops-intake-platform/blob/main/docs/runbook.md"

  # Common table settings. Point-in-time recovery is deliberately OFF for the
  # ledger and prompts (both are reconstructible) and ON for the two tables holding
  # data that cannot be recreated: results, and human corrections.
  server_side_encryption = {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }
}

# ---------------------------------------------------------------------------
# 1. Idempotency ledger.
#
# The claim record. NO TTL, deliberately: expiring an idempotency record re-opens
# the duplicate window for any redelivery after the TTL, and each item is ~200
# bytes. Trading correctness for that storage would be a bad deal. See
# docs/decisions.md.
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "ledger" {
  name         = "${local.table_prefix}-ledger"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "idempotency_key"

  attribute {
    name = "idempotency_key"
    type = "S"
  }

  server_side_encryption {
    enabled     = local.server_side_encryption.enabled
    kms_key_arn = local.server_side_encryption.kms_key_arn
  }

  tags = merge(var.tags, { Name = "${local.table_prefix}-ledger" })
}

# ---------------------------------------------------------------------------
# 2. Results store. Auto-approved and human-approved outcomes share this table,
# distinguished by the `outcome` attribute, so consumers read one place and the
# auto-approval rate M4 reports is a single aggregation.
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "results" {
  name         = "${local.table_prefix}-results"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "correlation_id"

  attribute {
    name = "correlation_id"
    type = "S"
  }

  attribute {
    name = "outcome"
    type = "S"
  }

  attribute {
    name = "completed_at"
    type = "S"
  }

  # Lets M4 count auto-approved vs human-approved over a time window without
  # scanning the table. A Scan-based metric would get slower and more expensive as
  # the table grows, which is exactly when the metric matters most.
  global_secondary_index {
    name            = "outcome-completed_at-index"
    hash_key        = "outcome"
    range_key       = "completed_at"
    projection_type = "KEYS_ONLY"
  }

  # This is the record of what the platform decided. It cannot be regenerated.
  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = local.server_side_encryption.enabled
    kms_key_arn = local.server_side_encryption.kms_key_arn
  }

  tags = merge(var.tags, { Name = "${local.table_prefix}-results" })
}

# ---------------------------------------------------------------------------
# 3. Review queue. Holds the task token, so a leak here is a capability leak —
# which is why the review API looks tokens up rather than accepting them.
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "review_queue" {
  name         = "${local.table_prefix}-review-queue"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "correlation_id"

  attribute {
    name = "correlation_id"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "queued_at"
    type = "S"
  }

  # The reviewer-facing "what is waiting for me" query. Without this index the
  # review UI would Scan, and a growing queue would get slower to display precisely
  # when there is a backlog to clear.
  global_secondary_index {
    name            = "status-queued_at-index"
    hash_key        = "status"
    range_key       = "queued_at"
    projection_type = "ALL"
  }

  server_side_encryption {
    enabled     = local.server_side_encryption.enabled
    kms_key_arn = local.server_side_encryption.kms_key_arn
  }

  tags = merge(var.tags, { Name = "${local.table_prefix}-review-queue" })
}

# ---------------------------------------------------------------------------
# 4. Corrections — labelled training data.
#
# Point-in-time recovery ON: this is the one table whose contents represent human
# labour that cannot be recreated. Losing a document is recoverable by
# redelivering it; losing a reviewer's corrections is not.
#
# The range key is reviewed_at so the same document corrected twice (a review, then
# a later re-review) keeps both records rather than overwriting the first. An
# overwrite would silently destroy the audit trail of a changed decision.
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "corrections" {
  name         = "${local.table_prefix}-corrections"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "correlation_id"
  range_key    = "reviewed_at"

  attribute {
    name = "correlation_id"
    type = "S"
  }

  attribute {
    name = "reviewed_at"
    type = "S"
  }

  attribute {
    name = "corrected_class"
    type = "S"
  }

  # M5 reads corrections by class to assemble retraining data and to compute the
  # per-class override rate its concept-drift proxy is built on.
  global_secondary_index {
    name            = "corrected_class-reviewed_at-index"
    hash_key        = "corrected_class"
    range_key       = "reviewed_at"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = local.server_side_encryption.enabled
    kms_key_arn = local.server_side_encryption.kms_key_arn
  }

  tags = merge(var.tags, { Name = "${local.table_prefix}-corrections" })
}

# ---------------------------------------------------------------------------
# 5. Prompts. One item per document class, rendered from schemas/*.json by
# `make seed-prompts`. This is what makes the extraction prompt DATA: adding a
# field or a class means editing one JSON file and re-seeding, with no code, ASL or
# Terraform change.
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "prompts" {
  name         = "${local.table_prefix}-prompts"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "document_class"

  attribute {
    name = "document_class"
    type = "S"
  }

  server_side_encryption {
    enabled     = local.server_side_encryption.enabled
    kms_key_arn = local.server_side_encryption.kms_key_arn
  }

  tags = merge(var.tags, { Name = "${local.table_prefix}-prompts" })
}

# ---------------------------------------------------------------------------
# Dead-letter queue.
#
# SQS rather than only a log line or a DynamoDB table: a queue is something a human
# can drain and replay. A log search tells you a document failed; a queue lets you
# reprocess it.
# ---------------------------------------------------------------------------
resource "aws_sqs_queue" "dead_letter" {
  name                              = "${local.table_prefix}-dlq"
  message_retention_seconds         = var.dead_letter_retention_days * 24 * 60 * 60
  kms_master_key_id                 = var.kms_key_arn
  kms_data_key_reuse_period_seconds = 300

  # Long enough for an operator to inspect a message and decide before it becomes
  # visible to another consumer.
  visibility_timeout_seconds = 300

  tags = merge(var.tags, { Name = "${local.table_prefix}-dlq" })
}

# An alarm on queue depth, because a dead-letter queue nobody watches is a
# silent data-loss channel. Threshold 1: any dead-lettered document is worth
# knowing about at this volume. M4 attaches the SNS action.
resource "aws_cloudwatch_metric_alarm" "dead_letter_not_empty" {
  alarm_name = "${local.table_prefix}-dlq-not-empty"
  alarm_description = join(" ", [
    "One or more documents failed intake and are sitting in the dead-letter queue.",
    "WHAT BREAKS: those documents have no result and no review task. They are not",
    "lost — the queue retains them for 14 days — but they are not processed either.",
    "FIRST RESPONSE: read one message. Each carries correlation_id, the failing",
    "state, the error cause and a pointer to the source object, which is enough to",
    "diagnose without re-running. Fix forward, then replay the queue.",
    "NOTE: review tasks that time out after 7 days also land here, and those are",
    "documents a human was meant to look at — check the failing state before",
    "assuming a technical fault.",
    local.runbook_note,
  ])

  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.dead_letter.name
  }

  # An earlier revision left these empty with a comment saying "M4 attaches the SNS
  # action". M4 shipped and nothing wired it, so the single data-safety alarm in the
  # system notified nobody while appearing in the generated inventory as configured.
  alarm_actions = var.alarm_sns_topic_arns
  ok_actions    = var.alarm_sns_topic_arns

  tags = merge(var.tags, { measures = "data safety", Name = "${local.table_prefix}-dlq-not-empty" })
}
