# -------------------------------------------------
# -- provider
# -------------------------------------------------

variable "provider_credentials" {
  default = "var .HOME/.aws/.credentials"
}

variable "user_profile" {
  default = "default"
}

# # -------------------------------------------------
# # -- aws secret manager
# # -------------------------------------------------

# variable "aws_secret_manager_secret_name_1" {
#   type        = string
#   description = "secret name"
#   default     = "car_pricer/autotrader/api_access"
# }

# variable "aws_secret_manager_secret_name_2" {
#   type        = string
#   description = "secret name"
#   default     = "car_pricer/cat/api_access"
# }

# variable "aws_secret_manager_secret_value_1" {
#   type        = string
#   description = "secret value"
#   default     = <<EOF
#     {
#       "key": "value"
#     }
#     EOF

#   # default = {
#   #   key           = "key",
#   #   secret        = "secret",
#   #   advertiser_id = "advertiser_id"
#   # }
# }

# -------------------------------------------------
# -- aws security group
# -------------------------------------------------

variable "aws_security_group_name" {
  type        = string
  description = "the security group name"
  default     = "vehicle_list_evaluator_sg"
}

variable "aws_security_group_description" {
  type        = string
  description = "the security group description"
  default     = "vehicle_list_evaluator security group"
}

# -------------------------------------------------
# -- aws iam role
# -------------------------------------------------

variable "aws_lambda_iam_role_name_1" {
  type        = string
  description = "lambda iam role name"
  default     = "aws_lambda_vehicle_list_evaluator_role"
}

variable "aws_lambda_iam_policy_name_1" {
  type        = string
  description = "lambda iam policy name"
  default     = "aws_lambda_vehicle_list_evaluator_access_policy"
}

# -------------------------------------------------
# -- aws rds
# -------------------------------------------------

variable "aws_rds_cluster_identifier" {
  type        = string
  description = "rds cluster identifier"
  default     = "car-pricer-rds-cluster"
}

# -------------------------------------------------
# -- aws lambda
# -------------------------------------------------

variable "aws_lambda_function_name_1" {
  type        = string
  description = "lambda function name"
  default     = "vehicle_list_router"
}

variable "aws_lambda_memory_size_1" {
  type        = string
  description = "lambda memory size"
  default     = "3072"
}

variable "aws_lambda_timeout_1" {
  type        = string
  description = "lambda timeout"
  default     = "600"
}

variable "aws_lambda_function_name_2" {
  type        = string
  description = "lambda function name"
  default     = "vehicle_list_evaluator"
}

variable "aws_lambda_memory_size_2" {
  type        = string
  description = "lambda memory size"
  default     = "3072"
}

variable "aws_lambda_timeout_2" {
  type        = string
  description = "lambda timeout"
  default     = "600"
}


# -------------------------------------------------
# -- amazon api gateway
# -------------------------------------------------

variable "api_gateway_name" {
  type        = string
  description = "api gateway name"
  default     = "vehicle_list_evaluator_api"
}

variable "api_gateway_description" {
  type        = string
  description = "api gateway description"
  default     = "vehicle list evaluator api"
}

variable "api_gateway_binary_media_types_list" {
  type        = list(string)
  description = "api gateway binary media types"
  default     = ["*/*"]
}

variable "api_gateway_parent_resource_name" {
  type        = string
  description = "api gateway parent resource name"
  default     = "{proxy+}"
}

variable "api_gateway_method" {
  type        = string
  description = "api gateway child resource name"
  default     = "ANY"
}

variable "api_gateway_authorization" {
  type        = string
  description = "api gateway authorization"
  default     = "NONE"
}

variable "api_gateway_custom_domain_name_dev" {
  type        = string
  description = "api gateway custom domain name for production"
  default     = "dev-uk-vle.aramis-group.eu"
}

variable "api_gateway_custom_domain_name_prod" {
  type        = string
  description = "api gateway custom domain name for development"
  default     = "uk-vle.aramis-group.eu"
}

# -------------------------------------------------
# -- amazon route53
# -------------------------------------------------

variable "aws_route53_root_domain_hosted_zone" {
  type        = string
  description = "root domain hosted zone"
  default     = "aramis-group.eu"
}

# -------------------------------------------------
# -- tags
# -------------------------------------------------

variable "tags_country" {
  default = "united_kingdom"
}

variable "tags_application" {
  default = "vehicle_list_evaluator"
}

variable "tags_team" {
  default = "car_pricer"
}

variable "tags_billing_service" {
  default = "data"
}

variable "tags_billing_platform" {
  default = "carsupermarket"
}

variable "tags_managed_by" {
  default = "terraform"
}
