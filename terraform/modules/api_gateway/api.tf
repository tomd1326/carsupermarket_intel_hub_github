# -------------------------------------------------
# -- aws cloudwatch logs
# -------------------------------------------------

resource "aws_cloudwatch_log_group" "log_group" {
  name              = "/aws/apigateway/${var.api_gateway_name}_${var.company}_${var.env}"
  retention_in_days = 60

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

# -------------------------------------------------
# -- amazon api gateway
# -------------------------------------------------

# -------------------------------------------------
# -- amazon api gateway POST method Lambda proxy
# -------------------------------------------------

resource "aws_api_gateway_rest_api" "api" {
  name        = "${var.api_gateway_name}_${var.company}_${var.env}"
  description = var.api_gateway_description

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  binary_media_types = var.api_gateway_binary_media_types_list

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

resource "aws_api_gateway_resource" "parent_resource_name" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = var.api_gateway_parent_resource_name
}

resource "aws_api_gateway_method" "method_1" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.parent_resource_name.id
  http_method   = var.api_gateway_method
  authorization = var.api_gateway_authorization
}

resource "aws_api_gateway_integration" "lambda_integration_1" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_method.method_1.resource_id
  http_method = aws_api_gateway_method.method_1.http_method

  integration_http_method = "POST" # Lambda function do only accept POST
  type                    = "AWS_PROXY"
  uri                     = var.aws_lambda_invoke_arn
}

# -------------------------------------------------
# -- amazon api gateway deployment and permission
# -------------------------------------------------

resource "aws_api_gateway_deployment" "api_deployment" {
  depends_on = [
    aws_api_gateway_integration.lambda_integration_1,
  ]

  rest_api_id = aws_api_gateway_rest_api.api.id
  # stage_name  = var.env

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_integration.lambda_integration_1,
      aws_api_gateway_resource.parent_resource_name,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "stage" {
  deployment_id = aws_api_gateway_deployment.api_deployment.id
  rest_api_id   = aws_api_gateway_rest_api.api.id
  stage_name    = var.env

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.log_group.arn
    format          = jsonencode({ "requestId" : "$context.requestId", "resourcePath" : "$context.path", "status" : "$context.status", "integrationLatency" : "$context.integrationLatency", "responseLatency" : "$context.responseLatency" })
  }
}

resource "aws_lambda_permission" "lambda_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = var.aws_lambda_function_name
  principal     = "apigateway.amazonaws.com"

  source_arn = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}

# -------------------------------------------------
# -- amazon api gateway stage settings
# -------------------------------------------------

resource "aws_api_gateway_method_settings" "stage_settings" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  stage_name  = aws_api_gateway_stage.stage.stage_name
  method_path = "*/*"

  settings {
    metrics_enabled = true
  }
}

# -------------------------------------------------
# -- amazon api gateway custom domain name
# -------------------------------------------------


# create ssl certificate
resource "aws_acm_certificate" "certificate" {
  domain_name       = var.api_gateway_custom_domain_name
  validation_method = "DNS"
  key_algorithm     = "EC_prime256v1"

  tags = {
    Environment     = var.env
    Country         = var.tags_country
    Application     = var.tags_application
    Team            = var.tags_team
    BillingService  = var.tags_billing_service
    BillingPlatform = var.tags_billing_platform
    ManagedBy       = var.tags_managed_by
  }

  lifecycle {
    create_before_destroy = true
  }
}

# create sub domain hosted zone
resource "aws_route53_zone" "domain_hosted_zone_name" {
  name = var.api_gateway_custom_domain_name

  tags = {
    Environment     = var.env
    Country         = var.tags_country
    Application     = var.tags_application
    Team            = var.tags_team
    BillingService  = var.tags_billing_service
    BillingPlatform = var.tags_billing_platform
  }
}

# create CNAME record in sub domain hosted zone
resource "aws_route53_record" "domain_hosted_zone_cname_record" {

  for_each = {
    for dvo in aws_acm_certificate.certificate.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  name            = each.value.name
  records         = [each.value.record]
  ttl             = 60
  type            = each.value.type
  zone_id         = aws_route53_zone.domain_hosted_zone_name.zone_id
  allow_overwrite = true
}

# get root domain hosted zone
data "aws_route53_zone" "root_domain_hosted_zone" {
  name = var.aws_route53_root_domain_hosted_zone
}


# create NS record in root domain hosted zone for sub domain with sub domain nameservers
resource "aws_route53_record" "root_domain_hosted_zone_ns_record" {
  name            = aws_route53_zone.domain_hosted_zone_name.name
  ttl             = 172800
  type            = "NS"
  zone_id         = data.aws_route53_zone.root_domain_hosted_zone.zone_id
  records         = aws_route53_zone.domain_hosted_zone_name.name_servers
  allow_overwrite = true
}

# get validation status of certificate if certificate has been issued
resource "aws_acm_certificate_validation" "certificate_validation" {
  certificate_arn = aws_acm_certificate.certificate.arn
}

# create regional custom domain name
resource "aws_api_gateway_domain_name" "custom_domain" {
  depends_on = [
    aws_acm_certificate_validation.certificate_validation,
  ]

  regional_certificate_arn = aws_acm_certificate.certificate.arn
  domain_name              = var.api_gateway_custom_domain_name

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  tags = {
    Environment     = var.env
    Application     = var.tags_application
    Team            = var.tags_team
    BillingService  = var.tags_billing_service
    BillingPlatform = var.tags_billing_platform
  }
}

# create A record with api gw regional domain name for sub domain hosted zone
resource "aws_route53_record" "domain_hosted_zone_a_record" {
  depends_on = [
    aws_api_gateway_domain_name.custom_domain
  ]

  name    = aws_route53_zone.domain_hosted_zone_name.name
  type    = "A"
  zone_id = aws_route53_zone.domain_hosted_zone_name.zone_id

  alias {
    evaluate_target_health = true
    name                   = aws_api_gateway_domain_name.custom_domain.regional_domain_name
    zone_id                = aws_api_gateway_domain_name.custom_domain.regional_zone_id
  }
}

# create mapping between custom domain name and api gw
resource "aws_api_gateway_base_path_mapping" "mapping" {
  api_id      = aws_api_gateway_rest_api.api.id
  stage_name  = aws_api_gateway_stage.stage.stage_name
  domain_name = aws_api_gateway_domain_name.custom_domain.domain_name
}
