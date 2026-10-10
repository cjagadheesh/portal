# The role boxes run as. It exists only so the SSM agent can register the instance with
# Session Manager; that is how we get a shell without opening any inbound port.
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "box" {
  name               = "${var.name_prefix}-box"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

# The only permission a box gets. A compromised box can talk to SSM and nothing else,
# so it cannot touch other instances or the reaper.
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.box.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# EC2 attaches roles through an instance profile, not the role directly.
resource "aws_iam_instance_profile" "box" {
  name = "${var.name_prefix}-box"
  role = aws_iam_role.box.name
}

data "aws_partition" "current" {}

# The drain handler on a spot box writes one tag (portal:resumable) on its own home volume
# when the box is being reclaimed. That is the only thing the box can do beyond SSM: it can
# add that single tag key, and only to volumes that already carry portal:managed=true.
resource "aws_iam_role_policy" "box_mark_home_resumable" {
  name = "mark-home-volume-resumable"
  role = aws_iam_role.box.id
  policy = templatefile("${path.module}/box-policy.json.tftpl", {
    partition  = data.aws_partition.current.partition
    region     = var.region
    account_id = data.aws_caller_identity.current.account_id
  })
}
