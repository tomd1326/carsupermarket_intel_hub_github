output "aws_api_gateway_name" {
  value = aws_api_gateway_rest_api.api.name
}

output "aws_api_gateway_invoke_url" {
  value = aws_api_gateway_stage.stage.invoke_url
}

output "aws_api_gateway_stage_name" {
  value = aws_api_gateway_stage.stage.stage_name
}
