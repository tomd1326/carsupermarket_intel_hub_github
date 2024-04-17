resource "aws_iam_role" "role" {
  name               = "${var.iam_role_name}_${var.company}_${var.env}"
  assume_role_policy = file("${path.module}/roles/${var.iam_role_name}.json")

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

resource "aws_iam_policy" "policy" {
  name        = "${var.iam_policy_name}_${var.company}_${var.env}"
  description = "${var.iam_policy_name}_${var.company} access policy"
  policy      = file("${path.module}/policies/${var.iam_policy_name}.json")

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

resource "aws_iam_policy_attachment" "policy_attachment_to_role" {
  name       = "${var.iam_policy_name}_${var.company}"
  roles      = [aws_iam_role.role.name]
  policy_arn = aws_iam_policy.policy.arn
}
