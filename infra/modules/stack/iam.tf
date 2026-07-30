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
# The state-machine role is created now (Step Functions is core to M3/M5) but
# carries NO permission policy at all — see the note above the role itself.
# Actions against the SageMaker endpoint, Textract, Bedrock, the DynamoDB
# review table, and its own log group are added when those resources exist, so
# the policy never has to reference a resource that isn't real yet.
#
# Every service trust policy below carries aws:SourceAccount and a scoped
# aws:SourceArn. Without them, any principal in this account holding
# iam:PassRole on these roles could drive them from their own SageMaker job or
# state machine — the confused-deputy problem. The SourceArn patterns are
# intentionally wider than a single resource ARN because the resources they
# name do not exist until M1-M3; they still pin the account and region.

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
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:sagemaker:${var.region}:${local.account_id}:*"]
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
    sid    = "WriteModelArtifacts"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      # model.tar.gz is uploaded multipart, and a retried or interrupted upload
      # needs to abort and re-list its parts. Without these, a transient
      # failure mid-upload leaves the training job unable to recover.
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["${module.artifacts_bucket.bucket_arn}/*"]
  }

  statement {
    # SageMaker resolves a bucket's region before reading from it.
    sid       = "ResolveBucketRegions"
    effect    = "Allow"
    actions   = ["s3:GetBucketLocation"]
    resources = [module.raw_bucket.bucket_arn, module.processed_bucket.bucket_arn, module.artifacts_bucket.bucket_arn]
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
    # SageMaker creates a grant on the key to encrypt the training job's
    # attached storage volume and its output artifacts. Without CreateGrant the
    # first M1 training job fails with a KMS AccessDenied before it runs a line
    # of Python.
    #
    # Kept in its own statement because kms:GrantIsForAWSResource is only
    # present in the request context of the grant APIs. Attaching it to the
    # statement above would make the Bool test evaluate against a missing key
    # for Decrypt/Encrypt, silently denying them.
    sid       = "AllowAwsResourceGrantsOnProjectKey"
    effect    = "Allow"
    actions   = ["kms:CreateGrant"]
    resources = [module.kms.key_arn]

    condition {
      test     = "Bool"
      variable = "kms:GrantIsForAWSResource"
      values   = ["true"]
    }
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
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:sagemaker:${var.region}:${local.account_id}:*"]
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
    sid    = "WriteDataCapture"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["${module.data_capture_bucket.bucket_arn}/*"]
  }

  statement {
    sid       = "ResolveBucketRegions"
    effect    = "Allow"
    actions   = ["s3:GetBucketLocation"]
    resources = [module.artifacts_bucket.bucket_arn, module.data_capture_bucket.bucket_arn]
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
    # See the equivalent statement on the training role: the endpoint needs a
    # grant to encrypt its data-capture output with the CMK. Separate statement
    # because kms:GrantIsForAWSResource only exists in the grant APIs' context.
    sid       = "AllowAwsResourceGrantsOnProjectKey"
    effect    = "Allow"
    actions   = ["kms:CreateGrant"]
    resources = [module.kms.key_arn]

    condition {
      test     = "Bool"
      variable = "kms:GrantIsForAWSResource"
      values   = ["true"]
    }
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
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:states:${var.region}:${local.account_id}:stateMachine:${local.name_prefix}*"]
    }
  }
}

# Deliberately no permission policy at M0. An earlier revision attached the
# CloudWatch Logs *delivery* actions here, which AWS requires on Resource "*".
# That granted the ability to attach a resource policy to any log group in the
# account to a role that has nothing to run, because no state machine exists
# until M3 — a wildcard with no corresponding capability. Deleted; M3 re-adds
# it alongside the state machine that needs it. See docs/decisions.md,
# "Deleted over-engineering".

module "state_machine_role" {
  source = "../iam_role"

  role_name               = "${local.name_prefix}state-machine-${var.environment}"
  description             = "Step Functions execution role (component=state-machine, env=${var.environment}). Trust-only at M0; task-invoke and logging permissions added in M3/M5 alongside the resources they target."
  assume_role_policy_json = data.aws_iam_policy_document.state_machine_assume.json
  inline_policy_json      = null

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
      # Only the default branch and pull-request contexts, never "any ref".
      # `repo:<org>/<repo>:*` would let a workflow on any branch of a PUBLIC
      # repo mint credentials that can delete this environment's Terraform
      # state. M6 splits this further into an apply-capable role trusted only
      # by refs/heads/main and a plan-only role trusted by pull_request, at
      # which point the pull_request entry moves off this role entirely.
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_repository}:ref:refs/heads/main",
        "repo:${var.github_repository}:pull_request",
      ]
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
      # `env:/*` is required in addition to this environment's own key prefix:
      # Terraform's S3 backend enumerates workspaces during `init` by listing
      # under `env:/`, and a condition that omits it makes `terraform init`
      # fail with AccessDenied. Listing key names is not the sensitive
      # operation here — GetObject is, and that stays scoped below.
      values = ["envs/${var.environment}/*", "env:/*"]
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
