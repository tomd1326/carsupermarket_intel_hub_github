# -------------------------------------------------
# -- aws cloudwatch logs  
# -------------------------------------------------

resource "aws_cloudwatch_log_group" "log_group_function" {
  name              = "/aws/lambda/${var.aws_lambda_function_name}_${var.company}_${var.env}"
  retention_in_days = 60

  tags = {
    Environment     = var.env
    Country         = var.tags_country
    Application     = var.tags_application
    Team            = var.tags_team
    BillingService  = var.tags_billing_service
    BillingPlatform = var.tags_billing_platform
  }
}

# -------------------------------------------------
# -- aws lambda
# -------------------------------------------------

resource "aws_lambda_function" "function" {
  function_name = "${var.aws_lambda_function_name}_${var.company}_${var.env}"
  package_type  = "Image"
  image_uri     = var.aws_lambda_image_uri
  role          = var.aws_lambda_iam_role_arn
  memory_size   = var.aws_lambda_memory_size
  timeout       = var.aws_lambda_timeout

  vpc_config {
    subnet_ids         = toset(var.subnet_list)
    security_group_ids = toset(var.security_group_list)
  }

  environment {
    variables = {
      variables = var.aws_environment_variable
    }
  }

  tags = {
    Environment     = var.env
    Country         = var.tags_country
    Application     = var.tags_application
    Team            = var.tags_team
    BillingService  = var.tags_billing_service
    BillingPlatform = var.tags_billing_platform
    ManagedBy       = var.tags_managed_by
  }

  # The code is deployed by the deploy_docker_image job. plan_tf runs alongside
  # build_docker_image, so it reads the previous image's digest; applying that
  # plan rolled the Lambda back to old code (2026-10-19). Terraform only sets
  # the image when it first creates the function.
  lifecycle {
    ignore_changes = [image_uri]
  }
}
