locals {
  function_name = "${var.name_prefix}-reaper"
}

data "archive_file" "reaper" {
  type        = "zip"
  source_file = "${path.module}/handler.py"
  output_path = "${path.module}/build/reaper.zip"
}

# Created here (not by Lambda on first run) so retention is set from the very first log line.
resource "aws_cloudwatch_log_group" "reaper" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "reaper" {
  function_name    = local.function_name
  role             = aws_iam_role.reaper.arn
  runtime          = "python3.12"
  handler          = "handler.lambda_handler"
  architectures    = ["arm64"] # cheaper per ms than x86, and the code is pure Python
  timeout          = 60
  memory_size      = 128
  filename         = data.archive_file.reaper.output_path
  source_code_hash = data.archive_file.reaper.output_base64sha256

  environment {
    variables = {
      REAPER_DRY_RUN                   = tostring(var.dry_run)
      REAPER_INVALID_TAG_GRACE_MINUTES = tostring(var.invalid_tag_grace_minutes)
    }
  }

  depends_on = [aws_cloudwatch_log_group.reaper, aws_iam_role_policy.reaper]
}
