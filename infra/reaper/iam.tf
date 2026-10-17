data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "reaper" {
  name               = "${var.name_prefix}-reaper"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# The whole permission set lives in policy.json.tftpl so a test can read the exact same
# file and prove what the reaper can and cannot do. Describe cannot be limited by tag
# (AWS does not support resource-level permissions for it), but terminate can: an instance
# without portal:managed=true is not terminable by this role, whatever the code does.
resource "aws_iam_role_policy" "reaper" {
  name = "reap-managed-instances"
  role = aws_iam_role.reaper.id
  policy = templatefile("${path.module}/policy.json.tftpl", {
    partition     = data.aws_partition.current.partition
    region        = var.region
    account_id    = data.aws_caller_identity.current.account_id
    log_group_arn = aws_cloudwatch_log_group.reaper.arn
  })
}
