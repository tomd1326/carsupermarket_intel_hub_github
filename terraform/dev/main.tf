# -------------------------------------------------
# -- provider
# -------------------------------------------------

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.15.0"
    }
  }
}

provider "aws" {
  region              = local.provider_region
}

# -------------------------------------------------
# -- workspace backend
# -------------------------------------------------

terraform {
  backend "s3" {
    bucket         = "aa-terraform-running-tfstate-prod"
    key            = "uk/carsupermarket/carsupermarket_vehicle_list_evaluator/terraform.tfstate"
    region         = "eu-west-1"
    dynamodb_table = "aa-terraform-running-tfstate-locks"
    encrypt        = true
  }
}


# -------------------------------------------------
# -- aws vpc - security groups
# -------------------------------------------------

data "aws_security_group" "rds_sg" {
  name = "aurora_pg_core_cluster_sg"
}

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

# add ingress rule to allow traffic from the application security group to the rds security group
resource "aws_vpc_security_group_ingress_rule" "ingress_rule_1" {
  security_group_id = data.aws_security_group.rds_sg.id
  description       = var.aws_security_group_name
  ip_protocol       = "tcp"
  from_port         = "5432"
  to_port           = "5432"
  referenced_security_group_id = module.security_group.aws_security_group_id
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
  cluster_identifier = "${local.env}-${var.aws_rds_cluster_identifier}"
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

# -------------------------------------------------
# -- aws api gateway
# -------------------------------------------------

module "api_gateway_1" {
  source  = "../modules/api_gateway"
  env     = local.env
  company = var.company

  api_gateway_name                    = var.api_gateway_name
  api_gateway_description             = var.api_gateway_description
  api_gateway_binary_media_types_list = var.api_gateway_binary_media_types_list
  api_gateway_parent_resource_name    = var.api_gateway_parent_resource_name
  api_gateway_method                  = var.api_gateway_method
  api_gateway_authorization           = var.api_gateway_authorization
  api_gateway_custom_domain_name      = local.api_gateway_custom_domain_name

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
