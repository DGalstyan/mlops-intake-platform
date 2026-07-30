# infra/modules/observability/dashboard.tf — the dashboard, in four sections.
#
# The brief is "a dashboard a non-engineer could read". Three things follow from
# taking that literally:
#
#   1. Every section has a markdown header saying what it answers in plain language,
#      so a reader does not have to infer intent from metric names.
#   2. Business outcome comes FIRST, and CPU appears nowhere. A dashboard whose top
#      row is CPU utilisation is the named point-loser; the top row here answers "is
#      the platform doing its job?".
#   3. Rates are shown as percentages via metric math, not as raw counters a reader
#      has to divide in their head.

locals {
  # Single-element gate lists. `cond ? [widget] : []` fails validation because
  # Terraform requires both branches of a conditional to share a type, and a tuple of
  # widget objects is not the same type as an empty tuple. Iterating over a 0-or-1
  # element list yields the same effect with consistent typing.
  if_state_machine = local.has_state_machine ? [1] : []
  if_dlq           = local.has_dlq ? [1] : []
  if_endpoint      = local.has_endpoint ? [1] : []

  # Full width is 24 columns.
  full  = 24
  half  = 12
  third = 8

  dashboard_body = jsonencode({
    start          = "-PT3H"
    periodOverride = "inherit"
    widgets = concat(
      # ===================================================================
      # 1. BUSINESS OUTCOME — first, deliberately.
      # ===================================================================
      [
        {
          type   = "text"
          x      = 0
          y      = 0
          width  = local.full
          height = 2
          properties = {
            markdown = join("\n", [
              "# Document Intake — ${var.environment}",
              "",
              "## 1. Business outcome — is the platform doing its job?",
              "**Auto-approval rate** is the headline number: the share of documents that needed no human.",
              "A fall here means more human work, and is the earliest business-visible sign of model or input change.",
            ])
          }
        },
        {
          type   = "metric"
          x      = 0
          y      = 2
          width  = local.third
          height = 6
          properties = {
            title  = "Auto-approval rate (%) — higher is better"
            view   = "timeSeries"
            region = var.region
            stat   = "Sum"
            period = 900
            yAxis  = { left = { min = 0, max = 100 } }
            metrics = [
              [{ expression = "100 * m2 / m1", label = "Auto-approval rate", id = "e1", region = var.region }],
              [local.ns, "DocumentsProcessed", "Environment", var.environment, { id = "m1", visible = false }],
              [local.ns, "AutoApproved", "Environment", var.environment, { id = "m2", visible = false }],
            ]
            annotations = {
              horizontal = [
                {
                  label = "alarm threshold"
                  value = var.auto_approval_rate_floor_percent
                  fill  = "below"
                },
              ]
            }
          }
        },
        {
          type   = "metric"
          x      = local.third
          y      = 2
          width  = local.third
          height = 6
          properties = {
            title   = "Documents processed vs sent to a human"
            view    = "timeSeries"
            stacked = true
            region  = var.region
            stat    = "Sum"
            period  = 900
            metrics = [
              [local.ns, "AutoApproved", "Environment", var.environment, { label = "Auto-approved" }],
              [local.ns, "HumanReviewed", "Environment", var.environment, { label = "Went to a human" }],
              [local.ns, "DeadLettered", "Environment", var.environment, { label = "Failed (dead-lettered)" }],
            ]
          }
        },
        {
          type   = "metric"
          x      = 2 * local.third
          y      = 2
          width  = local.third
          height = 6
          properties = {
            title  = "Throughput by document class"
            view   = "timeSeries"
            region = var.region
            stat   = "Sum"
            period = 900
            metrics = [
              for class_name in ["invoice", "medical_report", "id_document", "correspondence"] :
              [local.ns, "DocumentsProcessed", "Environment", var.environment, "DocumentClass", class_name, { label = class_name }]
            ]
          }
        },
      ],

      # ===================================================================
      # 2. MODEL HEALTH — the proxies, labelled as proxies.
      # ===================================================================
      [
        {
          type   = "text"
          x      = 0
          y      = 8
          width  = local.full
          height = 3
          properties = {
            markdown = join("\n", [
              "## 2. Model health — is the model still good?",
              "There is **no ground truth in production**, so nothing here measures accuracy directly. These are *proxies*:",
              "",
              "- **Human override rate** — of the documents a reviewer looked at, how often the model was wrong. The primary proxy. **Blind to confidently-wrong documents**, because nobody reviews those.",
              "- **Confidence p10 / p50** — the model's own certainty. A sinking p10 with unchanged inputs is the concept-drift signal.",
              "- **Schema-failure rate** — how often the *extraction* model returns something invalid. A model signal, not a pipeline one.",
            ])
          }
        },
        {
          type   = "metric"
          x      = 0
          y      = 11
          width  = local.third
          height = 6
          properties = {
            title  = "Human override rate (%) — of reviewed documents only"
            view   = "timeSeries"
            region = var.region
            period = 3600
            yAxis  = { left = { min = 0, max = 100 } }
            metrics = [
              [{ expression = "100 * m2 / m1", label = "Override rate (reviewed slice)", id = "e1", region = var.region }],
              [local.ns, "HumanReviewed", "Environment", var.environment, { id = "m1", stat = "Sum", visible = false }],
              [local.ns, "HumanOverride", "Environment", var.environment, { id = "m2", stat = "Sum", visible = false }],
            ]
            annotations = {
              horizontal = [
                { label = "alarm threshold", value = var.human_override_rate_ceiling_percent, fill = "above" },
              ]
            }
          }
        },
        {
          type   = "metric"
          x      = local.third
          y      = 11
          width  = local.third
          height = 6
          properties = {
            title  = "Classifier confidence — p10 moves before p50"
            view   = "timeSeries"
            region = var.region
            period = 3600
            yAxis  = { left = { min = 0, max = 1 } }
            metrics = [
              [local.ns, "Confidence", "Environment", var.environment, { stat = "p10", label = "p10 (tail)" }],
              [local.ns, "Confidence", "Environment", var.environment, { stat = "p50", label = "p50 (median)" }],
              [local.ns, "Confidence", "Environment", var.environment, { stat = "Average", label = "mean" }],
            ]
            annotations = {
              horizontal = [
                { label = "auto-approve threshold (0.80)", value = 0.80 },
                { label = "p10 alarm", value = var.confidence_p10_floor, fill = "below" },
              ]
            }
          }
        },
        {
          type   = "metric"
          x      = 2 * local.third
          y      = 11
          width  = local.third
          height = 6
          properties = {
            title  = "Extraction schema-failure rate (%)"
            view   = "timeSeries"
            region = var.region
            period = 900
            metrics = [
              [{ expression = "100 * m2 / m1", label = "Schema-failure rate", id = "e1", region = var.region }],
              [local.ns, "DocumentsProcessed", "Environment", var.environment, { id = "m1", stat = "Sum", visible = false }],
              [local.ns, "SchemaValidationFailure", "Environment", var.environment, { id = "m2", stat = "Sum", visible = false }],
            ]
            annotations = {
              horizontal = [
                { label = "alarm threshold", value = var.schema_failure_rate_ceiling_percent, fill = "above" },
              ]
            }
          }
        },
      ],

      # ===================================================================
      # 3. PIPELINE HEALTH — system, not model.
      # ===================================================================
      [
        {
          type   = "text"
          x      = 0
          y      = 17
          width  = local.full
          height = 2
          properties = {
            markdown = join("\n", [
              "## 3. Pipeline health — is the machinery working?",
              "These are **system-health** metrics. They can all be green while the model is quietly wrong — which is why section 2 exists.",
              "A non-empty dead-letter queue is the one panel here that means data is at risk.",
            ])
          }
        },
      ],
      [for _ in local.if_state_machine : {
        type   = "metric"
        x      = 0
        y      = 19
        width  = local.half
        height = 6
        properties = {
          title  = "End-to-end time (p50 / p95) — includes human wait"
          view   = "timeSeries"
          region = var.region
          period = 900
          metrics = [
            ["AWS/States", "ExecutionTime", "StateMachineArn", local.state_machine_arn, { stat = "p50", label = "p50" }],
            ["AWS/States", "ExecutionTime", "StateMachineArn", local.state_machine_arn, { stat = "p95", label = "p95" }],
          ]
          annotations = {
            horizontal = [
              { label = "p95 alarm", value = var.end_to_end_latency_p95_seconds * 1000, fill = "above" },
            ]
          }
        }
      }],
      [for _ in local.if_state_machine : {
        type   = "metric"
        x      = local.half
        y      = 19
        width  = local.half
        height = 6
        properties = {
          title  = "Executions: succeeded / failed / timed out"
          view   = "timeSeries"
          region = var.region
          stat   = "Sum"
          period = 300
          metrics = [
            ["AWS/States", "ExecutionsSucceeded", "StateMachineArn", local.state_machine_arn, { label = "Succeeded" }],
            ["AWS/States", "ExecutionsFailed", "StateMachineArn", local.state_machine_arn, { label = "Failed" }],
            ["AWS/States", "ExecutionsTimedOut", "StateMachineArn", local.state_machine_arn, { label = "Timed out" }],
          ]
        }
      }],
      [for _ in local.if_dlq : {
        type   = "metric"
        x      = 0
        y      = 25
        width  = local.half
        height = 5
        properties = {
          title  = "Dead-letter queue depth — any non-zero value needs attention"
          view   = "singleValue"
          region = var.region
          stat   = "Maximum"
          period = 300
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", var.dead_letter_queue_name, { label = "Documents awaiting replay" }],
          ]
        }
      }],
      [for _ in local.if_endpoint : {
        type   = "metric"
        x      = local.half
        y      = 25
        width  = local.half
        height = 5
        properties = {
          title  = "Endpoint: model latency p99 and 5xx"
          view   = "timeSeries"
          region = var.region
          period = 300
          metrics = [
            ["AWS/SageMaker", "ModelLatency", "EndpointName", var.endpoint_name, "VariantName", "AllTraffic", { stat = "p99", label = "ModelLatency p99 (us)" }],
            ["AWS/SageMaker", "ModelInvocation5XXErrors", "EndpointName", var.endpoint_name, "VariantName", "AllTraffic", { stat = "Sum", label = "5xx", yAxis = "right" }],
          ]
        }
      }],

      # ===================================================================
      # 4. COST
      # ===================================================================
      [
        {
          type   = "text"
          x      = 0
          y      = 30
          width  = local.full
          height = 3
          properties = {
            markdown = join("\n", [
              "## 4. Cost — what is this costing per document?",
              "**Estimated cost per document** is metric math over *real emitted token counts* times published prices",
              "(`config/prices.json`, retrieved **${local.prices.retrieved}**, ${local.prices.region}).",
              "",
              "Covers **Bedrock only**. Textract is ~$${local.prices.textract.detect_document_text_usd_per_1k_pages}/1k pages,",
              "and the endpoint bills ~$${local.prices.sagemaker.realtime_ml_t3_medium_usd_per_hour}/hour whether or not it serves traffic — that standing charge usually dominates a low-volume run.",
            ])
          }
        },
        {
          type   = "metric"
          x      = 0
          y      = 33
          width  = local.half
          height = 6
          properties = {
            title  = "Estimated Bedrock cost per document (USD)"
            view   = "timeSeries"
            region = var.region
            period = 3600
            metrics = [
              [{
                expression = "(m1 * ${local.bedrock_input_usd_per_token} + m2 * ${local.bedrock_output_usd_per_token}) / m3"
                label      = "USD per document"
                id         = "e1"
                region     = var.region
              }],
              [local.ns, "LLMInputTokens", "Environment", var.environment, { id = "m1", stat = "Sum", visible = false }],
              [local.ns, "LLMOutputTokens", "Environment", var.environment, { id = "m2", stat = "Sum", visible = false }],
              [local.ns, "DocumentsProcessed", "Environment", var.environment, { id = "m3", stat = "Sum", visible = false }],
            ]
            annotations = {
              horizontal = [
                { label = "alarm threshold", value = var.estimated_cost_per_document_ceiling_usd, fill = "above" },
              ]
            }
          }
        },
        {
          type   = "metric"
          x      = local.half
          y      = 33
          width  = local.half
          height = 6
          properties = {
            title  = "Token volume per document — the lever behind the cost"
            view   = "timeSeries"
            region = var.region
            period = 3600
            metrics = [
              [{ expression = "m1 / m3", label = "Input tokens / doc", id = "e1", region = var.region }],
              [{ expression = "m2 / m3", label = "Output tokens / doc", id = "e2", region = var.region }],
              [local.ns, "LLMInputTokens", "Environment", var.environment, { id = "m1", stat = "Sum", visible = false }],
              [local.ns, "LLMOutputTokens", "Environment", var.environment, { id = "m2", stat = "Sum", visible = false }],
              [local.ns, "DocumentsProcessed", "Environment", var.environment, { id = "m3", stat = "Sum", visible = false }],
            ]
          }
        },
      ],
    )
  })

  state_machine_arn = local.has_state_machine ? "arn:aws:states:${var.region}:${data.aws_caller_identity.current.account_id}:stateMachine:${var.state_machine_name}" : ""
}

resource "aws_cloudwatch_dashboard" "intake" {
  dashboard_name = "${local.name}-intake"
  dashboard_body = local.dashboard_body
}
