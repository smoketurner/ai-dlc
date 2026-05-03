################################################################################
# Hosted zone for the project. Created once in bootstrap so dev + prod envs
# can share it via `data "aws_route53_zone"`. Delegate the zone from the
# parent (smoketurner.com) by adding the NS records output here to the
# parent zone's nameserver records.
################################################################################

resource "aws_route53_zone" "this" {
  name    = var.dns_zone_name
  comment = "Hosted zone for ${var.project} (envs share this zone via data lookup)."

  tags = {
    Name      = var.dns_zone_name
    Project   = var.project
    Component = "bootstrap"
  }
}
