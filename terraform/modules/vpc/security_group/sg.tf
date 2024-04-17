# -------------------------------------------------
# -- aws security group
# -------------------------------------------------

# get default vpc
data "aws_vpc" "default" {
  default = true
}

resource "aws_security_group" "sg" {
  name        = "${var.aws_security_group_name}_${var.company}_${var.env}"
  description = var.aws_security_group_description
  vpc_id      = data.aws_vpc.default.id

  tags = {
    Environment     = var.env
    Country         = var.tags_country
    Application     = var.tags_application
    Team            = var.tags_team
    BillingService  = var.tags_billing_service
    BillingPlatform = var.tags_billing_platform
    ManagedBy       = var.tags_managed_by
  }
}

resource "aws_vpc_security_group_ingress_rule" "ingress_rule" {
  security_group_id            = aws_security_group.sg.id
  referenced_security_group_id = aws_security_group.sg.id
  ip_protocol                  = -1
}

resource "aws_vpc_security_group_egress_rule" "egress_rule" {
  security_group_id = aws_security_group.sg.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = -1
}
