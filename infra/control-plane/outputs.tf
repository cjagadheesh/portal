# `portal init` reads these and stores them in ~/.portal/state.json for `portal up`.
output "instance_profile_name" {
  description = "Instance profile to attach to every box."
  value       = aws_iam_instance_profile.box.name
}

output "security_group_id" {
  description = "Egress-only security group for every box."
  value       = aws_security_group.box.id
}

output "budget_name" {
  description = "Name of the monthly cost budget."
  value       = aws_budgets_budget.monthly.name
}

output "reaper_function_name" {
  description = "Name of the reaper Lambda (layer 3 of the shutdown design)."
  value       = module.reaper.function_name
}
