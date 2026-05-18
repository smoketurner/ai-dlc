# Terraform & Deployment

All infrastructure is managed with Terraform (`hashicorp/aws ~> 6`).

## Terraform Layout

```
terraform/
  modules/          # Reusable modules (one per concern)
    agents/         # AgentCore Runtime, IAM, gateways, identity, memory, tools
    auth/           # Cognito user pool + OIDC
    ci_cd/          # GitHub OIDC, IAM roles for CI
    dashboard/      # API Gateway + Lambda for the FastAPI dashboard
    improvement/    # Retrospector dispatcher, consolidation schedule
    messaging/      # EventBridge bus, schema registry, SQS queues, Pipes
    observability/  # CloudWatch dashboards, alarms, SNS topics
    pipeline/       # Lambdas (state_router, event_projector, entry_adapter)
    registry/       # ECR repos, creation template, lifecycle policies
    state/          # DynamoDB tables, GSIs
  envs/
    dev/            # Dev environment composition
  bootstrap/        # One-time S3 + DDB state backend
```

## State Backend

S3 + DynamoDB state backend with partial config. `backend.hcl` and `terraform.tfvars` are gitignored.

```hcl
# terraform/envs/dev/backend.tf (partial config)
terraform {
  backend "s3" {
    # bucket, profile supplied via -backend-config
  }
}
```

CI supplies `bucket` from the `TF_STATE_BUCKET` repo variable and leaves `profile` empty so the S3 backend falls through to OIDC env-var credentials.

## GitHub Actions Workflows

### `images-build.yml`

Triggers on push to `main` (paths: `agents/**`, `packages/common/**`, `pyproject.toml`, `uv.lock`).

Matrix builds all agents in parallel:
1. Checkout + configure AWS OIDC credentials
2. Login to ECR
3. Docker buildx: `linux/arm64`, push `{sha}` + `latest` tags
4. Roll AgentCore Runtime to the new image digest via `update-agent-runtime` API

### `terraform-apply.yml`

Triggers on push to `main` (paths: `terraform/**`, `lambdas/*/src/**`, `packages/common/src/**`).

Steps:
1. OIDC assume role
2. Pull SAM Lambda build image (`public.ecr.aws/sam/build-python3.14:latest-arm64`)
3. `terraform init -backend-config="bucket=$TF_STATE_BUCKET" -backend-config="profile="`
4. `terraform plan -out=tfplan`
5. `terraform apply -auto-approve tfplan`

Dev auto-applies. Prod requires manual approval via GitHub Environments (not yet configured).

### `ci.yml`

Standard CI: lint, type-check, unit tests.

## ECR

- Repos auto-created on first push via `aws_ecr_repository_creation_template.agents`
- Naming: `ai-dlc/<agent_name>` (e.g., `ai-dlc/architect`)
- Immutable tags (except `latest` which is overwritten on every push)
- Lifecycle policy for image cleanup
- AgentCore-pull cross-account policy attached

## AgentCore Runtime

All agents use `container_configuration` (not `code_configuration`) because Python 3.14 is outside AgentCore's supported range (3.10-3.13).

Runtime naming: `ai_dlc_<env>_<agent>` (underscores). The `images-build` workflow rolls each runtime to the new image digest after push via the AgentCore `update-agent-runtime` API.

Configuration in terraform: `terraform/modules/agents/runtime.tf` defines the runtimes with role ARN, network configuration, protocol configuration, environment variables, and container URI.

## Target-Repo Prerequisites

- **"Automatically delete head branches"**: must be enabled. Impl branches (`aidlc/impl/{run_id}`) are removed on PR merge.
- **Branch protection on `aidlc/impl/*`**: optional but recommended. The platform does not server-side-merge into impl branches; a human merges the impl PR via the GitHub UI.

## Local Development

### Agents

Each agent runs unchanged on a laptop:

```bash
cd agents/architect && uv sync && AIDLC_ENV=dev uv run python -m architect.app
# Hit it on :8080/invocations with the Bedrock AgentCore session header.
```

### Dashboard

```bash
cd services/dashboard
uv run uvicorn dashboard.app:app --reload --port 8080
```

Set `AIDLC_AUTH=disabled` to bypass Cognito OIDC locally.

### Terraform (Local)

On first checkout:

```bash
cd terraform/envs/dev
cp terraform.tfvars.example terraform.tfvars   # fill in real values
terraform init -reconfigure \
  -backend-config="bucket=<state-bucket-name>" \
  -backend-config="profile=aidlc-admin"
```

`-reconfigure` is required the first time after the backend was converted to a partial config. The state bucket name and AWS profile can also live in a gitignored `backend.hcl` invoked via `-backend-config=backend.hcl`.

## Lambda Deployment

Lambdas are deployed by Terraform via `terraform-aws-modules/lambda/aws` with `source_path`. The module hashes function source + requirements and rebuilds the zip on every plan. Any code change under `lambdas/*/src/` or `packages/common/src/` (a workspace dep many Lambdas bundle) requires a terraform-apply run.

The SAM Lambda build image (`public.ecr.aws/sam/build-python3.14:latest-arm64`) is used by the Lambda module for Docker-based builds, matching the `linux/arm64` runtime architecture.
