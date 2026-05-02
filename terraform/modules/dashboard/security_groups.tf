################################################################################
# Two security groups:
#   * alb     — ingress 80/443 from 0.0.0.0/0
#   * tasks   — ingress 8080 only from the ALB SG
################################################################################

resource "aws_security_group" "alb" {
  name        = "${local.prefix}-dashboard-alb"
  description = "Internet to dashboard ALB."
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTPS from the internet."
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTP (redirected to HTTPS) when an ACM cert is present, plain HTTP listener otherwise."
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "ALB to ECS tasks."
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-dashboard-alb"
    Component = "dashboard"
  })
}

resource "aws_security_group" "tasks" {
  name        = "${local.prefix}-dashboard-tasks"
  description = "ALB to dashboard ECS tasks."
  vpc_id      = var.vpc_id

  ingress {
    description     = "ALB to task port."
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    description = "Tasks to AWS service endpoints."
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-dashboard-tasks"
    Component = "dashboard"
  })
}
