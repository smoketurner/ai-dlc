################################################################################
# ECS Fargate cluster + task definition + service + autoscaling.
#
# Image deploys are handled out-of-band by the dashboard-build workflow:
# after pushing a new image to ECR, the workflow runs
#   aws ecs update-service --force-new-deployment
# which makes ECS pull the current `:latest` and roll the service. Terraform
# just owns the long-lived task definition + service; image SHAs never live
# in state. First-time bootstrap requires the build workflow to publish an
# image before terraform apply, otherwise the task fails to start.
################################################################################

resource "aws_ecs_cluster" "this" {
  name = local.cluster_name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = merge(var.tags, {
    Name      = local.cluster_name
    Component = "dashboard"
  })
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    base              = 1
    weight            = 100
    capacity_provider = "FARGATE"
  }
}

resource "aws_iam_role" "task" {
  name               = "${local.prefix}-dashboard-task"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json

  tags = merge(var.tags, {
    Name      = "${local.prefix}-dashboard-task"
    Component = "dashboard"
  })
}

resource "aws_iam_role_policy" "task_inline" {
  name   = "task-inline"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_inline.json
}

resource "aws_iam_role" "execution" {
  name               = "${local.prefix}-dashboard-execution"
  assume_role_policy = data.aws_iam_policy_document.execution_assume.json

  tags = merge(var.tags, {
    Name      = "${local.prefix}-dashboard-execution"
    Component = "dashboard"
  })
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = data.aws_iam_policy.ecs_task_execution.arn
}

resource "aws_cloudwatch_log_group" "task" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days

  tags = merge(var.tags, {
    Name      = local.log_group_name
    Component = "dashboard"
  })
}

resource "aws_ecs_task_definition" "this" {
  family                   = local.task_family
  cpu                      = tostring(var.task_cpu)
  memory                   = tostring(var.task_memory_mb)
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  task_role_arn            = aws_iam_role.task.arn
  execution_role_arn       = aws_iam_role.execution.arn

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([
    {
      name      = "dashboard"
      image     = "${var.ecr_repository_url}:latest"
      essential = true

      portMappings = [
        { containerPort = 8080, protocol = "tcp" },
      ]

      environment = [
        { name = "AIDLC_ENV", value = var.env },
        { name = "AWS_REGION", value = local.aws_region },
        { name = "AIDLC_BUS_NAME", value = var.bus_name },
        { name = "AIDLC_RUNS_TABLE", value = var.runs_table },
        { name = "AIDLC_APPROVALS_TABLE", value = var.approvals_table },
        { name = "AIDLC_IDEMPOTENCY_TABLE", value = var.idempotency_table },
        { name = "AIDLC_ARTIFACTS_BUCKET", value = var.artifacts_bucket },
        { name = "AIDLC_HITL_HANDLER_FUNCTION", value = var.hitl_handler_function_name },
        { name = "AIDLC_GITHUB_WEBHOOK_SECRET_ID", value = var.github_webhook_secret_id },
        { name = "AIDLC_COGNITO_REGION", value = local.aws_region },
        { name = "AIDLC_COGNITO_USER_POOL_ID", value = var.cognito_user_pool_id },
        { name = "AIDLC_COGNITO_CLIENT_ID", value = var.cognito_user_pool_client_id },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.task.name
          awslogs-region        = local.aws_region
          awslogs-stream-prefix = "dashboard"
        }
      }

      healthCheck = {
        command     = ["CMD-SHELL", "python -c 'import socket,sys; s=socket.socket(); s.settimeout(2); s.connect((\"127.0.0.1\",8080)); sys.exit(0)'"]
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }
    },
  ])

  tags = merge(var.tags, {
    Name      = local.task_family
    Component = "dashboard"
  })
}

resource "aws_ecs_service" "this" {
  name            = local.service_name
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = var.desired_count

  # The dashboard-build workflow scales via update-service and the
  # autoscaling scheduled actions adjust desired_count out-of-band; let
  # those changes stand without terraform fighting on the next apply.
  lifecycle {
    ignore_changes = [desired_count]
  }

  capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 100
    base              = 0
  }

  # Public subnets + public IP because there's no NAT — the task uses its
  # own ENI for outbound AWS API calls. Inbound is still gated by the
  # tasks SG, which only allows traffic from the ALB SG.
  network_configuration {
    subnets          = var.public_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.this.arn
    container_name   = "dashboard"
    container_port   = 8080
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  tags = merge(var.tags, {
    Name      = local.service_name
    Component = "dashboard"
  })

  depends_on = [
    aws_lb_listener.https,
    aws_lb_listener.http,
  ]
}

resource "aws_appautoscaling_target" "this" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.this.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.min_capacity
  max_capacity       = var.max_capacity
}

resource "aws_appautoscaling_policy" "cpu" {
  name               = "${local.service_name}-cpu60"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.this.service_namespace
  resource_id        = aws_appautoscaling_target.this.resource_id
  scalable_dimension = aws_appautoscaling_target.this.scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value = 60.0
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    scale_in_cooldown  = 120
    scale_out_cooldown = 60
  }
}

# Scheduled scale-to-0 between 23:00–07:00 America/New_York (DST handled
# automatically by the timezone field). Two actions: down at 23:00, up at 07:00.

resource "aws_appautoscaling_scheduled_action" "scale_down_overnight" {
  name               = "${local.service_name}-scale-down-overnight"
  service_namespace  = aws_appautoscaling_target.this.service_namespace
  resource_id        = aws_appautoscaling_target.this.resource_id
  scalable_dimension = aws_appautoscaling_target.this.scalable_dimension
  schedule           = "cron(0 23 * * ? *)"
  timezone           = "America/New_York"

  scalable_target_action {
    min_capacity = 0
    max_capacity = 0
  }
}

resource "aws_appautoscaling_scheduled_action" "scale_up_morning" {
  name               = "${local.service_name}-scale-up-morning"
  service_namespace  = aws_appautoscaling_target.this.service_namespace
  resource_id        = aws_appautoscaling_target.this.resource_id
  scalable_dimension = aws_appautoscaling_target.this.scalable_dimension
  schedule           = "cron(0 7 * * ? *)"
  timezone           = "America/New_York"

  scalable_target_action {
    min_capacity = var.min_capacity
    max_capacity = var.max_capacity
  }
}
