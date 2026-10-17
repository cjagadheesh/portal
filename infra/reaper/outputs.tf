output "function_name" {
  description = "Name of the reaper Lambda."
  value       = aws_lambda_function.reaper.function_name
}

output "log_group_name" {
  description = "CloudWatch log group holding every reaper decision."
  value       = aws_cloudwatch_log_group.reaper.name
}
