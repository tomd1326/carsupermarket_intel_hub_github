# -------------------------------------------------
# -- aws secret manager
# -------------------------------------------------

variable "env" {}

variable "company" {}

variable "aws_secret_manager_secret_name" {}

variable "aws_secret_manager_secret_value" {}

# -------------------------------------------------
# -- tags
# -------------------------------------------------

variable "tags_country" {}

variable "tags_application" {}

variable "tags_team" {}

variable "tags_billing_service" {}

variable "tags_billing_platform" {}

variable "tags_managed_by" {}
