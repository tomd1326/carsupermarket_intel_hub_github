# -------------------------------------------------
# -- aws secret manager
# -------------------------------------------------

resource "aws_secretsmanager_secret" "secret_1" {
  name                    = "${var.env}/${var.company}/${var.aws_secret_manager_secret_name}"
  recovery_window_in_days = "0"

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

resource "aws_secretsmanager_secret_version" "secret_version_1" {
  secret_id     = aws_secretsmanager_secret.secret.id
  secret_string = var.aws_secret_manager_secret_value
}
