# infra/modules/stack/iam.tf — one IAM role per component, each scoped to
# only the resources that exist in this milestone (M0: KMS key, ECR repo,
# the four buckets, and the role's own CloudWatch log group prefix).
#
# Lambda roles are deliberately NOT created here: no Lambda function exists
# yet (M3 introduces them). Creating a role today with a policy scoped to a
# function that doesn't exist would mean either granting nothing useful or
# guessing at a resource ARN — the milestone that creates each Lambda also
# creates and scopes its role, following this same pattern.
#
# The state-machine role is created now (Step Functions is core to M3/M5)
# but its permission policy is limited to what exists today: its own log
# group. Actions against the SageMaker endpoint, Textract, Bedrock, and the
# DynamoDB review table are added when those resources exist, so the policy
# never has to reference a resource that isn't real yet.

# ---------------------------------------------------------------------------
# training role — assumed by the SageMaker Training Job.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "training_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["sagemaker.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "training_permissions" {
  statement {
    sid       = "ListInputBuckets"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [module.raw_bucket.bucket_arn, module.processed_bucket.bucket_arn]
  }

  statement {
    sid       = "ReadTrainingData"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${module.raw_bucket.bucket_arn}/*", "${module.processed_bucket.bucket_arn}/*"]
  }

  statement {
    sid       = "ListArtifactsBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [module.artifacts_bucket.bucket_arn]
  }

  statement {
    sid       = "WriteModelArtifacts"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${module.artifacts_bucket.bucket_arn}/*"]
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
    resources = [module.kms.key_arn]
  }

  statement {
    sid       = "PullTrainingImage"
    effect    = "Allow"
    actions   = ["ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage", "ecr:BatchCheckLayerAvailability"]
    resources = [module.ecr.repository_arn]
  }

  statement {
    # ECR requires this action's resource to be "*" — GetAuthorizationToken
    # is an account-level, pre-auth call that has no resource-level
    # permissions in AWS's IAM model (documented AWS API restriction, not a
    # scoping choice we are making).
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "TrainingLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = [
      "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/sagemaker/TrainingJobs/${local.name_prefix}training-${var.environment}*:*",
    ]
  }
}

module "training_role" {
  source = "../iam_role"

  role_name               = "${local.name_prefix}training-${var.environment}"
  description             = "SageMaker Training Job execution role (component=training, env=${var.environment})."
  assume_role_policy_json = data.aws_iam_policy_document.training_assume.json
  inline_policy_json      = data.aws_iam_policy_document.training_permissions.json

  tags = merge(local.common_tags, { component = "training" })
}

# ---------------------------------------------------------------------------
# endpoint role — assumed by the SageMaker real-time endpoint.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "endpoint_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["sagemaker.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "endpoint_permissions" {
  statement {
    sid       = "ListArtifactsBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [module.artifacts_bucket.bucket_arn]
  }

  statement {
    sid       = "ReadModelArtifacts"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${module.artifacts_bucket.bucket_arn}/*"]
  }

  statement {
    sid       = "ListDataCaptureBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [module.data_capture_bucket.bucket_arn]
  }

  statement {
    sid       = "WriteDataCapture"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${module.data_capture_bucket.bucket_arn}/*"]
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
    resources = [module.kms.key_arn]
  }

  statement {
    sid       = "PullInferenceImage"
    effect    = "Allow"
    actions   = ["ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage", "ecr:BatchCheckLayerAvailability"]
    resources = [module.ecr.repository_arn]
  }

  statement {
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # see EcrAuth note on the training role above.
  }

  statement {
    sid    = "EndpointLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = [
      "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/sagemaker/Endpoints/${local.name_prefix}endpoint-${var.environment}*:*",
    ]
  }
}

module "endpoint_role" {
  source = "../iam_role"

  role_name               = "${local.name_prefix}endpoint-${var.environment}"
  description             = "SageMaker real-time endpoint execution role (component=endpoint, env=${var.environment})."
  assume_role_policy_json = data.aws_iam_policy_document.endpoint_assume.json
  inline_policy_json      = data.aws_iam_policy_document.endpoint_permissions.json

  tags = merge(local.common_tags, { component = "endpoint" })
}

# ---------------------------------------------------------------------------
# state-machine role — assumed by the Step Functions "intake" / "retrain"
# state machines (M3/M5). Trust-only role for now: no Textract/Bedrock/
# SageMaker-invoke/DynamoDB permissions are granted until those resources
# exist and can be named by ARN (see file header note).
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "state_machine_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "state_machine_permissions" {
  statement {
    sid    = "StateMachineLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:DescribeLogGroups",
      "logs:DescribeResourcePolicies",
      "logs:GetLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:UpdateLogDelivery",
    ]
    # The CloudWatch Logs *delivery* API (used by Step Functions execution
    # logging) does not support resource-level permissions — AWS requires
    # Resource "*" for these specific actions (documented AWS restriction:
    # https://docs.aws.amazon.com/step-functions/latest/dg/cw-logs.html).
    # This is the one place in this stack's IAM policies where that applies;
    # it is not a substitute for scoping PutLogEvents, which is scoped below.
    resources = ["*"]
  }

  statement {
    sid    = "StateMachineOwnLogGroup"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/vendedlogs/states/${local.name_prefix}intake-${var.environment}*:*",
    ]
  }
}

module "state_machine_role" {
  source = "../iam_role"

  role_name               = "${local.name_prefix}state-machine-${var.environment}"
  description             = "Step Functions execution role (component=state-machine, env=${var.environment}). Trust-only at M0; task-invoke permissions added in M3/M5 alongside the resources they target."
  assume_role_policy_json = data.aws_iam_policy_document.state_machine_assume.json
  inline_policy_json      = data.aws_iam_policy_document.state_machine_permissions.json

  tags = merge(local.common_tags, { component = "state-machine" })
}

# ---------------------------------------------------------------------------
# ci-deploy role — assumed by GitHub Actions via OIDC (no long-lived keys).
# Scoped to what M0 needs: push/pull this environment's ECR image, and
# read/write only this environment's key prefix in the shared state bucket.
# Broader `terraform apply` permissions for stack resources this role
# doesn't yet manage (SageMaker, Step Functions, ...) are added
# incrementally, per-milestone, scoped to intake-named resources — see
# docs/decisions.md "known gap" note.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "ci_deploy_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      # Any ref of this one repo. M6 should narrow this further (e.g. to
      # ref:refs/heads/main for apply-capable runs vs. pull_request for
      # plan-only runs) once the workflow structure is final.
      values = ["repo:${var.github_repository}:*"]
    }
  }
}

data "aws_iam_policy_document" "ci_deploy_permissions" {
  statement {
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # see EcrAuth note on the training role above.
  }

  statement {
    sid    = "PushInferenceImage"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
    ]
    resources = [module.ecr.repository_arn]
  }

  statement {
    sid       = "ListStateBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [local.state_bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["envs/${var.environment}/*"]
    }
  }

  statement {
    sid    = "ReadWriteOwnStateKey"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    # S3 native locking (use_lockfile) stores the lock as an object alongside
    # the state file under the same key prefix, so no separate DynamoDB
    # permissions are needed here.
    resources = ["${local.state_bucket_arn}/envs/${var.environment}/*"]
  }

  statement {
    # Pushing/pulling images to a KMS-encrypted ECR repository requires the
    # calling principal to have key access, not just the ECR API
    # permissions above (AWS ECR + customer-managed-KMS requirement).
    sid    = "UseProjectKeyForEcr"
    effect = "Allow"
    actions = [
      "kms:GenerateDataKey",
      "kms:Decrypt",
      "kms:DescribeKey",
    ]
    resources = [module.kms.key_arn]
  }
}

module "ci_deploy_role" {
  source = "../iam_role"

  role_name               = "${local.name_prefix}ci-deploy-${var.environment}"
  description             = "GitHub Actions OIDC deploy role (component=ci-deploy, env=${var.environment}). Repository-scoped trust, no long-lived AWS keys."
  assume_role_policy_json = data.aws_iam_policy_document.ci_deploy_assume.json
  inline_policy_json      = data.aws_iam_policy_document.ci_deploy_permissions.json

  tags = merge(local.common_tags, { component = "ci-deploy" })
}
