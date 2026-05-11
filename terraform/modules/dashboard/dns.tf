################################################################################
# ACM cert + Route 53 alias for the dashboard hostname.
#
# Only created when `dashboard_fqdn` and `route53_zone_id` are both set.
# The cert is regional (API Gateway HTTP custom domains require a cert in
# the same region as the API, not us-east-1).
################################################################################

resource "aws_acm_certificate" "this" {
  count = local.use_https ? 1 : 0

  domain_name       = var.dashboard_fqdn
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = merge(var.tags, {
    Name      = var.dashboard_fqdn
    Component = "dashboard"
  })
}

resource "aws_route53_record" "cert_validation" {
  for_each = local.use_https ? {
    for dvo in aws_acm_certificate.this[0].domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  } : {}

  zone_id         = var.route53_zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "this" {
  count = local.use_https ? 1 : 0

  certificate_arn         = aws_acm_certificate.this[0].arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

resource "aws_route53_record" "alias" {
  count = local.use_https ? 1 : 0

  zone_id = var.route53_zone_id
  name    = var.dashboard_fqdn
  type    = "A"

  alias {
    name                   = aws_apigatewayv2_domain_name.this[0].domain_name_configuration[0].target_domain_name
    zone_id                = aws_apigatewayv2_domain_name.this[0].domain_name_configuration[0].hosted_zone_id
    evaluate_target_health = false
  }
}
