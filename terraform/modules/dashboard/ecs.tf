################################################################################
# ECS Fargate cluster + task definition + service + autoscaling.
#
# Service is gated on `image_tag != ""` — first apply (no image yet) just
# creates the cluster + task IAM. Once CI pushes an image and the operator
# sets image_tag, the next apply provisions the task definition + service.
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
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_cloudwatch_log_group" "task" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = var.logs_kms_key_arn

  tags = merge(var.tags, {
    Name      = local.log_group_name
    Component = "dashboard"
  })
}

resource "aws_ecs_task_definition" "this" {
  count = local.has_image ? 1 : 0

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
      image     = "${var.ecr_repository_url}@${data.aws_ecr_image.dashboard[0].image_digest}"
      essential = true

      portMappings = [
        { containerPort = 8080, protocol = "tcp" },
      ]

      environment = [
        { name = "AIDLC_ENV", value = var.env },
        { name = "AWS_REGION", value = data.aws_region.current.region },
        { name = "AIDLC_BUS_NAME", value = var.bus_name },
        { name = "AIDLC_RUNS_TABLE", value = var.runs_table },
        { name = "AIDLC_APPROVALS_TABLE", value = var.approvals_table },
        { name = "AIDLC_IDEMPOTENCY_TABLE", value = var.idempotency_table },
        { name = "AIDLC_ARTIFACTS_BUCKET", value = var.artifacts_bucket },
        { name = "AIDLC_HITL_HANDLER_FUNCTION", value = var.hitl_handler_function_name },
        { name = "AIDLC_GITHUB_WEBHOOK_SECRET_ID", value = var.github_webhook_secret_id },
        { name = "AIDLC_COGNITO_REGION", value = data.aws_region.current.region },
        { name = "AIDLC_COGNITO_USER_POOL_ID", value = var.cognito_user_pool_id },
        { name = "AIDLC_COGNITO_CLIENT_ID", value = var.cognito_user_pool_client_id },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.task.name
          awslogs-region        = data.aws_region.current.region
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
  count = local.has_image ? 1 : 0

  name            = local.service_name
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this[0].arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
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
  count = local.has_image ? 1 : 0

  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.this[0].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.min_capacity
  max_capacity       = var.max_capacity
}

resource "aws_appautoscaling_policy" "cpu" {
  count = local.has_image ? 1 : 0

  name               = "${local.service_name}-cpu60"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.this[0].service_namespace
  resource_id        = aws_appautoscaling_target.this[0].resource_id
  scalable_dimension = aws_appautoscaling_target.this[0].scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value = 60.0
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    scale_in_cooldown  = 120
    scale_out_cooldown = 60
  }
}
