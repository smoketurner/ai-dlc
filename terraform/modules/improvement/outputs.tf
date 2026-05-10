output "retrospector_dispatcher_function_arn" {
  description = "Retrospector dispatcher Lambda ARN — empty when retrospector is not yet enabled."
  value       = try(module.retrospector_dispatcher[0].lambda_function_arn, "")
}
