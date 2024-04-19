# -------------------------------------------------
# -- cli input variables
# -------------------------------------------------

variable "company" {}

# -------------------------------------------------
# -- local variables
# -------------------------------------------------

locals {
  env                 = terraform.workspace == "prod" ? "prod" : "dev"
  provider_region     = terraform.workspace == "prod" ? "eu-west-1" : "us-east-2"
  private_subnet_id_1 = terraform.workspace == "prod" ? "subnet-0bd39ab9470f22c9e" : "subnet-02ff6f9ea16628639"
  private_subnet_id_2 = terraform.workspace == "prod" ? "subnet-0c9410ed3601a0eca" : "subnet-0398fc4aea95ec60c"

  api_gateway_custom_domain_name = terraform.workspace == "prod" ? var.api_gateway_custom_domain_name_prod : var.api_gateway_custom_domain_name_dev
}

# -------------------------------------------------
# -- provider
# -------------------------------------------------

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "4.64.0"
    }
  }
}

provider "aws" {
  shared_config_files = ["$HOME/.aws/credentials"]
  region              = local.provider_region
}

# -------------------------------------------------
# -- workspace backend
# -------------------------------------------------

terraform {
  backend "s3" {}
}

# -------------------------------------------------
# -- aws vpc - security groups
# -------------------------------------------------

module "security_group" {
  source  = "../modules/vpc/security_group"
  env     = local.env
  company = var.company

  aws_security_group_name        = var.aws_security_group_name
  aws_security_group_description = var.aws_security_group_description

  tags_country          = var.tags_country
  tags_application      = var.tags_application
  tags_team             = var.tags_team
  tags_billing_service  = var.tags_billing_service
  tags_billing_platform = var.tags_billing_platform
  tags_managed_by       = var.tags_managed_by

}

# -------------------------------------------------
# -- aws iam
# -------------------------------------------------

module "iam_role_lambda" {
  source  = "../modules/iam"
  env     = local.env
  company = var.company

  iam_role_name   = var.aws_lambda_iam_role_name_1
  iam_policy_name = var.aws_lambda_iam_policy_name_1

  tags_country          = var.tags_country
  tags_application      = var.tags_application
  tags_team             = var.tags_team
  tags_billing_service  = var.tags_billing_service
  tags_billing_platform = var.tags_billing_platform
  tags_managed_by       = var.tags_managed_by
}

# -------------------------------------------------
# -- aws ecr
# -------------------------------------------------

data "aws_ecr_repository" "image_repository_1" {
  name = "${var.aws_lambda_function_name_1}_repository_${var.company}_${local.env}"
}

data "aws_ecr_image" "image_container_1" {
  repository_name = data.aws_ecr_repository.image_repository_1.name
  image_tag       = "latest"
}

data "aws_ecr_repository" "image_repository_2" {
  name = "${var.aws_lambda_function_name_2}_repository_${var.company}_${local.env}"
}

data "aws_ecr_image" "image_container_2" {
  repository_name = data.aws_ecr_repository.image_repository_2.name
  image_tag       = "latest"
}

# -------------------------------------------------
# -- aws rds
# -------------------------------------------------

data "aws_rds_cluster" "rds_cluster" {
  cluster_identifier = "${var.aws_rds_cluster_identifier}-${local.env}"
}

# -------------------------------------------------
# -- aws lambda
# -------------------------------------------------

module "lambda_1" {
  source                   = "../modules/lambda/docker/vpc"
  env                      = local.env
  company                  = var.company
  aws_lambda_function_name = var.aws_lambda_function_name_1
  aws_lambda_memory_size   = var.aws_lambda_memory_size_1
  aws_lambda_timeout       = var.aws_lambda_timeout_1
  aws_lambda_image_uri     = "${data.aws_ecr_repository.image_repository_1.repository_url}@${data.aws_ecr_image.image_container_1.image_digest}"
  aws_lambda_iam_role_arn  = module.iam_role_lambda.iam_role_arn
  subnet_list              = [local.private_subnet_id_1, local.private_subnet_id_2]
  security_group_list      = [module.security_group.aws_security_group_id]
  aws_environment_variable = jsonencode(
    {
      "rds_cluster_endpoint" = data.aws_rds_cluster.rds_cluster.endpoint,
      "rds_reader_endpoint"  = data.aws_rds_cluster.rds_cluster.reader_endpoint
    }
  )
  tags_country          = var.tags_country
  tags_application      = var.tags_application
  tags_team             = var.tags_team
  tags_billing_service  = var.tags_billing_service
  tags_billing_platform = var.tags_billing_platform
  tags_managed_by       = var.tags_managed_by
}

module "lambda_2" {
  source                   = "../modules/lambda/docker/vpc"
  env                      = local.env
  company                  = var.company
  aws_lambda_function_name = var.aws_lambda_function_name_2
  aws_lambda_memory_size   = var.aws_lambda_memory_size_2
  aws_lambda_timeout       = var.aws_lambda_timeout_2
  aws_lambda_image_uri     = "${data.aws_ecr_repository.image_repository_2repository_url}@${data.aws_ecr_image.image_container_2.image_digest}"
  aws_lambda_iam_role_arn  = module.iam_role_lambda.iam_role_arn
  subnet_list              = [local.private_subnet_id_1, local.private_subnet_id_2]
  security_group_list      = [module.security_group.aws_security_group_id]
  aws_environment_variable = jsonencode(
    {
      "rds_cluster_endpoint" = data.aws_rds_cluster.rds_cluster.endpoint,
      "rds_reader_endpoint"  = data.aws_rds_cluster.rds_cluster.reader_endpoint
    }
  )
  tags_country          = var.tags_country
  tags_application      = var.tags_application
  tags_team             = var.tags_team
  tags_billing_service  = var.tags_billing_service
  tags_billing_platform = var.tags_billing_platform
  tags_managed_by       = var.tags_managed_by
}

# -------------------------------------------------
# -- aws api gateway
# -------------------------------------------------

module "api_gateway_1" {
  source  = "../modules/api_gateway"
  env     = local.env
  company = var.company

  api_gateway_name                 = var.api_gateway_name
  api_gateway_description          = var.api_gateway_description
  api_gateway_parent_resource_name = var.api_gateway_parent_resource_name
  api_gateway_method               = var.api_gateway_method
  api_gateway_authorization        = var.api_gateway_authorization
  api_gateway_custom_domain_name   = local.api_gateway_custom_domain_name

  aws_lambda_function_name = module.lambda_1.aws_lambda_function_name
  aws_lambda_invoke_arn    = module.lambda_1.aws_lambda_function_invoke_arn

  aws_route53_root_domain_hosted_zone = var.aws_route53_root_domain_hosted_zone

  tags_country          = var.tags_country
  tags_application      = var.tags_application
  tags_team             = var.tags_team
  tags_billing_service  = var.tags_billing_service
  tags_billing_platform = var.tags_billing_platform
  tags_managed_by       = var.tags_managed_by
}
