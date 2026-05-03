################################################################################
# ACM cert + Route 53 records for the dashboard hostname.
#
# Only created when `dashboard_fqdn` and `route53_zone_id` are both set —
# i.e., when the env wants HTTPS with a friendly hostname. The cert uses
# DNS validation against the provided hosted zone, and an A-alias is
# created from the FQDN to the ALB.
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
    name                   = aws_lb.this.dns_name
    zone_id                = aws_lb.this.zone_id
    evaluate_target_health = true
  }
}
