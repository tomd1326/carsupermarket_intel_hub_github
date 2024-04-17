# -------------------------------------------------
# -- aws lambda
# -------------------------------------------------

variable "env" {}

variable "company" {}

variable "aws_lambda_iam_role_arn" {}

variable "aws_lambda_function_name" {}

variable "aws_lambda_image_uri" {}

variable "aws_lambda_memory_size" {}

variable "aws_lambda_timeout" {}

variable "aws_environment_variable" {}

variable "subnet_list" {}

variable "security_group_list" {}

# -------------------------------------------------
# -- tags
# -------------------------------------------------

variable "tags_country" {}

variable "tags_application" {}

variable "tags_team" {}

variable "tags_billing_service" {}

variable "tags_billing_platform" {}

variable "tags_managed_by" {}
