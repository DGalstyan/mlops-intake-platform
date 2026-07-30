# infra/modules/iam_role — one role per component. Callers build the trust
# and permission policy JSON with aws_iam_policy_document data sources (never
# hand-written JSON with "*"), this module just wires them onto the role.

resource "aws_iam_role" "this" {
  name                 = var.role_name
  description          = var.description
  assume_role_policy   = var.assume_role_policy_json
  max_session_duration = 3600

  tags = var.tags
}

resource "aws_iam_role_policy" "this" {
  count  = var.inline_policy_json == null ? 0 : 1
  name   = var.role_name
  role   = aws_iam_role.this.id
  policy = var.inline_policy_json
}
