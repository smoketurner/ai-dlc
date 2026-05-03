# Terraform commands.
#
# Usage:
#   make plan                       # plan against $ENV (default: dev)
#   make ENV=prod plan
#   make apply                      # apply against $ENV
#   make fmt                        # terraform fmt -recursive on the whole tree
#   make validate                   # terraform validate against $ENV
#   make tflint                     # tflint the modules + envs
#   make zizmor                     # static-analyze the GH Actions workflows
#
# All non-trivial commands assume AWS credentials are already in the
# environment (e.g., from `aws sso login` or the GH OIDC role assumption in CI).

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

ENV ?= dev
ENV_DIR := envs/$(ENV)
TF     ?= terraform
TFLINT ?= tflint
ZIZMOR ?= uvx zizmor

.PHONY: help bootstrap-init bootstrap-apply init plan apply destroy fmt fmt-check validate tflint zizmor pull-build-image image-tags

# terraform-aws-modules/lambda v8 tries to build the docker_image locally
# when it isn't present, instead of pulling it. Pulling once up-front
# satisfies its `docker images` lookup and avoids the build path entirely.
LAMBDA_BUILD_IMAGE := public.ecr.aws/sam/build-python3.13:latest-arm64

# Mirror what .github/workflows/terraform-apply.yml does: query ECR for the
# most recently pushed image tag per repo and export TF_VAR_*_image_tag so
# `terraform plan` and `terraform apply` provision the runtime + ECS service
# pinned to those digests. Empty tag (no image yet) gates the resource off.
define image_tags_export
	for repo in architect implementer dashboard; do \
	  tag=$$(aws ecr describe-images \
	    --repository-name "ai-dlc/$$repo" \
	    --query 'reverse(sort_by(imageDetails,&imagePushedAt))[0].imageTags[0]' \
	    --output text 2>/dev/null || echo ""); \
	  [ "$$tag" = "None" ] && tag=""; \
	  printf 'export TF_VAR_%s_image_tag=%s\n' "$$repo" "$$tag"; \
	done
endef

help:                            ## Show this help.
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

bootstrap-init:                  ## One-time: terraform init the bootstrap (state backend).
	$(TF) -chdir=bootstrap init

bootstrap-apply:                 ## One-time: create the S3 tfstate bucket + DDB lock table.
	$(TF) -chdir=bootstrap apply

init:                            ## terraform init for the current ENV (reads bootstrap outputs for the S3 backend).
	@bucket=$$($(TF) -chdir=bootstrap output -raw tfstate_bucket); \
	$(TF) -chdir=$(ENV_DIR) init \
	  -backend-config="bucket=$$bucket"

pull-build-image:                ## Pull the SAM Lambda build image used by the lambda module.
	docker pull $(LAMBDA_BUILD_IMAGE)

image-tags:                      ## Print TF_VAR_* exports for the most-recently-pushed ECR image per repo.
	@$(image_tags_export)

plan: pull-build-image           ## terraform plan for the current ENV.
	@eval "$$($(image_tags_export))"; \
	$(TF) -chdir=$(ENV_DIR) plan -out=tfplan

apply: pull-build-image          ## terraform apply the most recent plan.
	@eval "$$($(image_tags_export))"; \
	$(TF) -chdir=$(ENV_DIR) apply tfplan

destroy:                         ## terraform destroy the current ENV (DANGEROUS).
	$(TF) -chdir=$(ENV_DIR) destroy

fmt:                             ## terraform fmt across the whole tree.
	$(TF) fmt -recursive

fmt-check:                       ## terraform fmt -check (no writes).
	$(TF) fmt -check -recursive

validate:                        ## terraform validate the current ENV.
	$(TF) -chdir=$(ENV_DIR) validate

tflint:                          ## tflint modules and envs.
	$(TFLINT) --recursive

zizmor:                          ## Static-analyze the GitHub Actions workflows.
	$(ZIZMOR) ../.github/workflows
