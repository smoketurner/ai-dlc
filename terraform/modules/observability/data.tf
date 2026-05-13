################################################################################
# Resolve current Bedrock service quotas at apply time. Bedrock does not
# publish to the AWS/Usage namespace, so SERVICE_QUOTA() metric math is
# unavailable; instead we look each quota up via the Service Quotas API
# and divide into it from the alarm's metric expression.
################################################################################

data "aws_servicequotas_service_quota" "bedrock" {
  for_each = local.bedrock_quota_lookups

  service_code = "bedrock"
  quota_code   = each.value.quota_code
}
