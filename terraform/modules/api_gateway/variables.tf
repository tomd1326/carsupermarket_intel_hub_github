# -------------------------------------------------
# -- amazon api gateway
# -------------------------------------------------

variable "env" {}

variable "company" {}

variable "api_gateway_name" {}

variable "api_gateway_description" {}

variable "api_gateway_parent_resource_name" {}

variable "api_gateway_method" {}

variable "api_gateway_authorization" {}

variable "api_gateway_custom_domain_name" {}

# -------------------------------------------------
# -- aws lambda
# -------------------------------------------------

variable "aws_lambda_invoke_arn" {}

variable "aws_lambda_function_name" {}

# -------------------------------------------------
# -- amazon route53
# -------------------------------------------------

variable "aws_route53_root_domain_hosted_zone" {}

# -------------------------------------------------
# -- tags
# -------------------------------------------------

variable "tags_country" {}

variable "tags_application" {}

variable "tags_team" {}

variable "tags_billing_service" {}

variable "tags_billing_platform" {}

variable "tags_managed_by" {}
