output "telemetry_function_arn" {
  description = "Telemetry Lambda ARN."
  value       = module.telemetry.lambda_function_arn
}

output "few_shot_miner_function_arn" {
  description = "Few-shot miner Lambda ARN."
  value       = module.few_shot_miner.lambda_function_arn
}

output "rejections_rule_arn" {
  description = "EventBridge rule that routes rejection events to the telemetry Lambda."
  value       = aws_cloudwatch_event_rule.rejections.arn
}

output "eval_runner_function_arn" {
  description = "Eval runner Lambda ARN."
  value       = module.eval_runner.lambda_function_arn
}

output "eval_state_machine_arn" {
  description = "Eval-runner Step Functions state machine ARN."
  value       = aws_sfn_state_machine.eval_runner.arn
}

output "eval_drift_alarm_arn" {
  description = "CloudWatch alarm ARN for eval-suite drift."
  value       = aws_cloudwatch_metric_alarm.eval_drift.arn
}
