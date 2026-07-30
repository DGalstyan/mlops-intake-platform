# infra/modules/intake/api.tf — the reviewer-facing correction API.
#
# HTTP API (API Gateway v2) rather than REST API: this needs one route with a Lambda
# integration, and the HTTP API is roughly a third of the cost with a fraction of the
# configuration surface. The REST API's features that would justify it — request
# validation models, usage plans, WAF — are either unnecessary here or belong in front
# of a real reviewer UI.
#
# AUTHORISATION IS NOT IMPLEMENTED, and that is a stated gap rather than an
# oversight. See the README's known gaps. The route is IAM-authorised so it is not
# open to the internet, but "which humans may review which documents" is a real
# access-control question this take-home does not answer, and pretending otherwise
# with a shared API key would be worse than naming it.

locals {
  api_name = "${var.name_prefix}review-${var.environment}"
}

resource "aws_apigatewayv2_api" "review" {
  name          = local.api_name
  protocol_type = "HTTP"
  description   = "Reviewer correction API. Submitting a correction resumes the waiting intake execution."

  tags = merge(var.tags, { Name = local.api_name })
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/apigateway/${local.api_name}"
  retention_in_days = var.log_retention_days
  tags              = merge(var.tags, { Name = "${local.api_name}-logs" })
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.review.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api.arn
    # Includes the correlation id from the path so an API call can be joined to the
    # document it acted on — the same id the state machine and the endpoint log.
    format = jsonencode({
      requestId        = "$context.requestId"
      correlationId    = "$context.path"
      httpMethod       = "$context.httpMethod"
      routeKey         = "$context.routeKey"
      status           = "$context.status"
      responseLength   = "$context.responseLength"
      integrationError = "$context.integrationErrorMessage"
      callerIdentity   = "$context.identity.caller"
      requestTime      = "$context.requestTime"
    })
  }

  default_route_settings {
    # A reviewer API is used by humans, so these limits are generous for real use and
    # low enough to bound the damage from a runaway script.
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }

  tags = merge(var.tags, { Name = "${local.api_name}-default" })
}

resource "aws_apigatewayv2_integration" "submit_correction" {
  api_id                 = aws_apigatewayv2_api.review.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.review_api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 29000
}

resource "aws_apigatewayv2_route" "submit_correction" {
  api_id    = aws_apigatewayv2_api.review.id
  route_key = "POST /reviews/corrections"
  target    = "integrations/${aws_apigatewayv2_integration.submit_correction.id}"

  # IAM authorisation: a caller must present SigV4 credentials. Not the same thing as
  # knowing *which* reviewer they are for audit purposes — the handler requires
  # reviewer_id in the body, which is trusted input and is the gap named above.
  authorization_type = "AWS_IAM"
}

resource "aws_lambda_permission" "api_invoke" {
  statement_id  = "AllowReviewApiInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.review_api.function_name
  principal     = "apigateway.amazonaws.com"

  # Scoped to this API and this route, not to apigateway.amazonaws.com generally.
  source_arn = "${aws_apigatewayv2_api.review.execution_arn}/*/POST/reviews/corrections"
}
