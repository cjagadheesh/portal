data "aws_vpc" "default" {
  count   = var.vpc_id == null ? 1 : 0
  default = true
}

locals {
  vpc_id = var.vpc_id != null ? var.vpc_id : data.aws_vpc.default[0].id
}

# Hard rule 1: this security group has NO ingress rules, and none may be added.
# Access is via SSM, which the agent opens outbound, so nothing needs to reach the box.
resource "aws_security_group" "box" {
  name        = "${var.name_prefix}-box"
  description = "portal boxes: egress only, no inbound rules (access is via SSM)"
  vpc_id      = local.vpc_id
}

# Declared as a separate rule because Terraform deletes AWS's default allow-all egress
# from groups it manages. Without this the box could not reach SSM, apt or docker hub.
resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.box.id
  description       = "All outbound: SSM, package mirrors, git, container registries"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}
