# Layer 3 runs on AWS's clock, not the laptop's. Five minutes bounds how long an expired
# box can outlive its expiry.
resource "aws_cloudwatch_event_rule" "every_five_minutes" {
  name                = "${local.function_name}-schedule"
  description         = "Run the portal reaper"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "reaper" {
  rule = aws_cloudwatch_event_rule.every_five_minutes.name
  arn  = aws_lambda_function.reaper.arn
}

resource "aws_lambda_permission" "from_eventbridge" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reaper.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.every_five_minutes.arn
}
