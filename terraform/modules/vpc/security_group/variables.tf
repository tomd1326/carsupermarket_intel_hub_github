# -------------------------------------------------
# -- global
# -------------------------------------------------

variable "env" {}

variable "company" {}

# -------------------------------------------------
# -- aws vpc
# -------------------------------------------------

variable "aws_security_group_name" {}

variable "aws_security_group_description" {}

# -------------------------------------------------
# -- tags
# -------------------------------------------------

variable "tags_country" {}

variable "tags_application" {}

variable "tags_team" {}

variable "tags_billing_service" {}

variable "tags_billing_platform" {}

variable "tags_managed_by" {}
