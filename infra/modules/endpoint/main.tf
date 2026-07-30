# infra/modules/endpoint — the real-time inference endpoint, its data capture,
# its autoscaling policy, and the canary deployment with automatic rollback.
#
# WHY REAL-TIME AND NOT SERVERLESS. The assignment encourages Serverless
# Inference on cost grounds, and for a mostly-idle graded run it would indeed be
# cheaper. It was rejected because it cannot satisfy three separate M2
# requirements at once:
#
#   1. Data capture is not supported on serverless endpoints. M5's drift
#      detection reads data-capture files as its only source of production
#      traffic, so choosing serverless deletes the input to a 20%-weighted
#      milestone.
#   2. Application Auto Scaling does not apply. Serverless scales on its own
#      concurrency model, so "autoscaling on a justified metric with a documented
#      target" has nothing to configure or justify.
#   3. Blue/green deployment guardrails — canary traffic shifting with alarm-driven
#      auto-rollback — are real-time only. That is M2's headline deliverable.
#
# The cost of that choice is a standing hourly charge, mitigated with the smallest
# viable instance and `make destroy`. Documented in the README cost table.

locals {
  model_name           = "${var.name_prefix}classifier-${var.environment}"
  endpoint_config_name = "${var.name_prefix}classifier-${var.environment}"
  endpoint_name        = "${var.name_prefix}classifier-${var.environment}"
  variant_name         = "AllTraffic"

  # Application Auto Scaling addresses a SageMaker variant by this composite id.
  autoscaling_resource_id = "endpoint/${local.endpoint_name}/variant/${local.variant_name}"
}

# ---------------------------------------------------------------------------
# Model — created from an APPROVED registry version, by ARN.
#
# Using the model package (rather than an image + model-data URL directly) is
# what ties the running endpoint back to a registry version, and therefore to
# the golden-set metrics and lineage recorded in M1.
# ---------------------------------------------------------------------------
resource "aws_sagemaker_model" "this" {
  name                     = local.model_name
  execution_role_arn       = var.execution_role_arn
  enable_network_isolation = false

  container {
    model_package_name = var.model_package_arn
  }

  tags = merge(var.tags, { Name = local.model_name })

  lifecycle {
    # A new model must exist before the endpoint config that references it can be
    # replaced, and the config cannot be deleted while the endpoint uses it.
    create_before_destroy = true
  }
}

# ---------------------------------------------------------------------------
# Endpoint configuration — data capture and the production variant.
# ---------------------------------------------------------------------------
resource "aws_sagemaker_endpoint_configuration" "this" {
  name        = local.endpoint_config_name
  kms_key_arn = var.kms_key_arn

  production_variants {
    variant_name           = local.variant_name
    model_name             = aws_sagemaker_model.this.name
    initial_instance_count = var.initial_instance_count
    instance_type          = var.instance_type
    initial_variant_weight = 1

    # Give the container time to load the model and pass its readiness probe
    # before SageMaker gives up on it. The default (300s) is ample here; stated
    # explicitly because a container that is killed mid-load looks like a broken
    # image rather than a slow one.
    container_startup_health_check_timeout_in_seconds = 300
  }

  # --- Data capture: the input to M5 ---
  data_capture_config {
    enable_capture              = true
    initial_sampling_percentage = var.data_capture_sampling_percentage
    destination_s3_uri          = "s3://${var.data_capture_bucket_name}/endpoint-capture/${var.environment}"
    kms_key_id                  = var.kms_key_arn

    # BOTH directions. Input alone supports input-drift detection only; the
    # output is what makes prediction drift and confidence decay measurable, and
    # those are two of the three drift signals M5 has to produce. Capturing only
    # requests is the most common way to discover at M5 that the data needed was
    # never recorded.
    capture_options {
      capture_mode = "Input"
    }
    capture_options {
      capture_mode = "Output"
    }

    capture_content_type_header {
      json_content_types = ["application/json"]
    }
  }

  tags = merge(var.tags, { Name = local.endpoint_config_name })

  lifecycle {
    create_before_destroy = true
  }
}

# ---------------------------------------------------------------------------
# Rollback alarms.
#
# These are defined here rather than in the M4 observability module because they
# are not observability — they are a control input. The endpoint's
# auto_rollback_configuration references them by name, so they must exist before
# the endpoint and must not be deleted while it exists. Keeping them beside the
# endpoint makes that dependency visible in one file.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "invocation_5xx" {
  alarm_name = "${var.name_prefix}endpoint-5xx-${var.environment}"
  alarm_description = join(" ", [
    "Endpoint returned 5xx errors. Drives automatic rollback of an in-flight",
    "deployment. Note the serving layer returns 4xx for malformed client input,",
    "so a 5xx here is a genuine endpoint fault, not a bad request."
  ])

  namespace   = "AWS/SageMaker"
  metric_name = "ModelInvocation5XXErrors"
  statistic   = "Sum"
  period      = 60
  # Two consecutive periods, not one: a single 5xx during instance replacement is
  # normal. Two in a row is a pattern. Combined with the canary bake time, this
  # still fires well within the deployment window.
  evaluation_periods  = 2
  threshold           = var.rollback_5xx_threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"

  # A variant serving zero traffic emits no datapoints. Treating missing data as
  # breaching would roll back every deployment during the gap before traffic
  # arrives.
  treat_missing_data = "notBreaching"

  dimensions = {
    EndpointName = local.endpoint_name
    VariantName  = local.variant_name
  }

  alarm_actions = var.alarm_sns_topic_arns
  ok_actions    = var.alarm_sns_topic_arns

  tags = merge(var.tags, { Name = "${var.name_prefix}endpoint-5xx-${var.environment}" })
}

resource "aws_cloudwatch_metric_alarm" "model_latency" {
  alarm_name = "${var.name_prefix}endpoint-latency-${var.environment}"
  alarm_description = join(" ", [
    "Endpoint p99 model latency exceeded its ceiling. Drives automatic rollback.",
    "Catches a variant that is functional but unusably slow — the failure an",
    "error-rate alarm alone never sees."
  ])

  namespace   = "AWS/SageMaker"
  metric_name = "ModelLatency"
  # p99, not Average: an average hides a tail that a p99 exposes, and the tail is
  # what a user experiences.
  extended_statistic = "p99"
  # ModelLatency is published in microseconds.
  period              = 60
  evaluation_periods  = 2
  threshold           = var.rollback_latency_threshold_ms * 1000
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    EndpointName = local.endpoint_name
    VariantName  = local.variant_name
  }

  alarm_actions = var.alarm_sns_topic_arns
  ok_actions    = var.alarm_sns_topic_arns

  tags = merge(var.tags, { Name = "${var.name_prefix}endpoint-latency-${var.environment}" })
}

# ---------------------------------------------------------------------------
# Endpoint — canary traffic shifting with automatic rollback.
#
# This block is M2's deliverable. `deployment_config` applies to *updates* of an
# existing endpoint, not to its initial creation: the first apply creates the
# endpoint outright, and every subsequent change to the endpoint configuration
# shifts traffic in the canary steps below while the alarms above are watched.
# ---------------------------------------------------------------------------
resource "aws_sagemaker_endpoint" "this" {
  name                 = local.endpoint_name
  endpoint_config_name = aws_sagemaker_endpoint_configuration.this.name

  deployment_config {
    blue_green_update_policy {
      traffic_routing_configuration {
        type                     = "CANARY"
        wait_interval_in_seconds = var.canary_bake_time_minutes * 60

        canary_size {
          type  = "CAPACITY_PERCENT"
          value = var.canary_traffic_percentage
        }
      }

      # How long the old (blue) fleet is kept alive after the shift completes.
      # This is the window in which a rollback is still instant, because the old
      # instances are still running and only need traffic pointed back at them.
      termination_wait_in_seconds = 600

      # Hard ceiling on the whole update. Without it a stuck deployment can hang
      # for hours in a state where neither variant is confirmed healthy.
      maximum_execution_timeout_in_seconds = 1800
    }

    auto_rollback_configuration {
      alarms {
        alarm_name = aws_cloudwatch_metric_alarm.invocation_5xx.alarm_name
      }
      alarms {
        alarm_name = aws_cloudwatch_metric_alarm.model_latency.alarm_name
      }
    }
  }

  tags = merge(var.tags, { Name = local.endpoint_name })

  depends_on = [
    # Named explicitly: auto_rollback_configuration references these alarms by
    # name, which Terraform cannot see as a dependency, so without this the
    # endpoint can be created before the alarms exist and the deployment would
    # have no rollback trigger.
    aws_cloudwatch_metric_alarm.invocation_5xx,
    aws_cloudwatch_metric_alarm.model_latency,
  ]
}

# ---------------------------------------------------------------------------
# Autoscaling.
#
# Target metric is SageMakerVariantInvocationsPerInstance rather than CPU. CPU
# utilisation on a sparse dot product barely moves under load — the endpoint
# queues before it saturates a core — so a CPU-based policy would scale far too
# late. Invocations per instance is the load signal that actually correlates with
# queueing here.
# ---------------------------------------------------------------------------
resource "aws_appautoscaling_target" "variant" {
  service_namespace  = "sagemaker"
  resource_id        = local.autoscaling_resource_id
  scalable_dimension = "sagemaker:variant:DesiredInstanceCount"
  min_capacity       = var.initial_instance_count
  max_capacity       = var.autoscaling_max_instances

  depends_on = [aws_sagemaker_endpoint.this]
}

resource "aws_appautoscaling_policy" "invocations_per_instance" {
  name               = "${var.name_prefix}endpoint-invocations-${var.environment}"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.variant.service_namespace
  resource_id        = aws_appautoscaling_target.variant.resource_id
  scalable_dimension = aws_appautoscaling_target.variant.scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value = var.autoscaling_target_invocations_per_instance

    predefined_metric_specification {
      predefined_metric_type = "SageMakerVariantInvocationsPerInstance"
    }

    scale_in_cooldown  = var.autoscaling_scale_in_cooldown_seconds
    scale_out_cooldown = var.autoscaling_scale_out_cooldown_seconds
  }
}
