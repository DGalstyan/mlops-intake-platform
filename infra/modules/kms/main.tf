# infra/modules/kms — one customer-managed key with an explicit,
# least-privilege key policy. No service principal is granted access here:
# S3/SageMaker only need the *calling role* to be a key user, which is
# enforced entirely through this key policy (no IAM-side grant needed beyond
# kms:Decrypt/GenerateDataKey on this key's ARN in the role's own policy).

data "aws_caller_identity" "current" {}

locals {
  # Explicit action list (no "kms:*") used by both the root and the
  # designated key-admin statements below.
  key_admin_actions = [
    "kms:CreateAlias",
    "kms:DeleteAlias",
    "kms:UpdateAlias",
    "kms:DescribeKey",
    "kms:GetKeyPolicy",
    "kms:GetKeyRotationStatus",
    "kms:ListAliases",
    "kms:ListGrants",
    "kms:ListKeyPolicies",
    "kms:ListResourceTags",
    "kms:PutKeyPolicy",
    "kms:TagResource",
    "kms:UntagResource",
    "kms:EnableKeyRotation",
    "kms:DisableKeyRotation",
    "kms:RotateKeyOnDemand",
    "kms:EnableKey",
    "kms:DisableKey",
    "kms:ScheduleKeyDeletion",
    "kms:CancelKeyDeletion",
    "kms:CreateGrant",
    "kms:RevokeGrant",
  ]
}

data "aws_iam_policy_document" "key_policy" {
  # Root account statement: required by AWS so that IAM policies (not just
  # this key policy) can also grant access. Without this, a locked-out key
  # policy can only be fixed by an account root user, not any IAM principal.
  # Deliberately enumerated (not "kms:*") to keep this policy free of
  # service-level action wildcards, matching the same admin action set below.
  statement {
    sid    = "EnableIAMUserPermissions"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
    actions   = local.key_admin_actions
    resources = ["*"] # see NOTE at bottom of file: resource-based policy, self-reference is impossible.
  }

  # Optional. Omitted entirely when no admin principals are supplied, because a
  # principals block with an empty identifiers list is an invalid policy. When
  # empty, administration still works through the root statement above via IAM
  # policies — the AWS-recommended default. Only pass durable IAM role/user
  # ARNs here, never an STS assumed-role *session* ARN: session ARNs embed an
  # ephemeral session name, so they either fail validation or leave a statement
  # that grants nothing and re-diffs on every apply.
  dynamic "statement" {
    for_each = length(var.key_admin_role_arns) > 0 ? [1] : []
    content {
      sid    = "KeyAdministration"
      effect = "Allow"
      principals {
        type        = "AWS"
        identifiers = var.key_admin_role_arns
      }
      actions   = local.key_admin_actions
      resources = ["*"] # see NOTE at bottom of file.
    }
  }

  dynamic "statement" {
    for_each = length(var.key_user_role_arns) > 0 ? [1] : []
    content {
      sid    = "ComponentKeyUsage"
      effect = "Allow"
      principals {
        type        = "AWS"
        identifiers = var.key_user_role_arns
      }
      actions = [
        "kms:Decrypt",
        "kms:Encrypt",
        "kms:GenerateDataKey",
        "kms:GenerateDataKeyWithoutPlaintext",
        "kms:DescribeKey",
      ]
      resources = ["*"] # see NOTE below.
    }
  }
}

# NOTE on "resources = [\"*\"]" in this file:
# This is a *key policy* (a resource-based policy attached to exactly one
# KMS key), not an IAM identity policy. Every statement here is implicitly
# scoped to aws_kms_key.this by virtue of being attached to it — AWS's own
# documentation and every published example use "*" for Resource inside a
# key policy, both because that is its accepted meaning ("this key") and
# because the key's own ARN cannot be referenced inside its own policy
# without a resource cycle (the key doesn't exist yet while its policy is
# being computed). No component role, service, or IAM policy in this repo
# uses "Resource": "*" — grep for `\"\*\"` outside this file to confirm.
resource "aws_kms_key" "this" {
  description             = var.description
  deletion_window_in_days = 7
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.key_policy.json

  tags = var.tags
}

resource "aws_kms_alias" "this" {
  name          = var.alias_name
  target_key_id = aws_kms_key.this.key_id
}
