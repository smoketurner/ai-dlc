################################################################################
# Application Load Balancer.
#
# When an ACM cert is provided, the default listener is HTTPS with Cognito
# OIDC authentication on the default action. Without a cert, the listener is
# HTTP — fine for dev where the dashboard URL is the ALB DNS name and we
# tolerate plain HTTP.
#
# A separate listener rule routes /webhooks/github to the same target group
# without authentication — the dashboard verifies the GitHub HMAC signature
# in-app, which is the trust boundary for that path.
################################################################################

resource "aws_lb" "this" {
  name               = local.alb_name
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids

  drop_invalid_header_fields = true
  enable_deletion_protection = false

  tags = merge(var.tags, {
    Name      = local.alb_name
    Component = "dashboard"
  })
}

resource "aws_lb_target_group" "this" {
  name        = "${local.prefix}-dash-tg"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  deregistration_delay = 30

  health_check {
    enabled             = true
    path                = "/healthz"
    port                = "traffic-port"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-dashboard-tg"
    Component = "dashboard"
  })
}

resource "aws_lb_listener" "https" {
  count = local.use_https ? 1 : 0

  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.this[0].certificate_arn

  default_action {
    type = "authenticate-cognito"

    authenticate_cognito {
      user_pool_arn              = var.cognito_user_pool_arn
      user_pool_client_id        = var.cognito_user_pool_client_id
      user_pool_domain           = var.cognito_user_pool_domain
      scope                      = "openid email profile"
      session_timeout            = 28800
      on_unauthenticated_request = "authenticate"
    }
  }

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}

resource "aws_lb_listener" "http" {
  count = local.use_https ? 0 : 1

  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}

# /webhooks/github bypasses Cognito auth — HMAC is the trust boundary.

resource "aws_lb_listener_rule" "webhooks_github_https" {
  count = local.use_https ? 1 : 0

  listener_arn = aws_lb_listener.https[0].arn
  priority     = 10

  condition {
    path_pattern {
      values = [local.webhook_path]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}

resource "aws_lb_listener_rule" "webhooks_github_http" {
  count = local.use_https ? 0 : 1

  listener_arn = aws_lb_listener.http[0].arn
  priority     = 10

  condition {
    path_pattern {
      values = [local.webhook_path]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}
