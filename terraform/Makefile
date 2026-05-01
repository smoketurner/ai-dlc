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

.PHONY: help bootstrap-init bootstrap-apply init plan apply destroy fmt fmt-check validate tflint zizmor

help:                            ## Show this help.
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

bootstrap-init:                  ## One-time: terraform init the bootstrap (state backend).
	$(TF) -chdir=bootstrap init

bootstrap-apply:                 ## One-time: create the S3 tfstate bucket + DDB lock table.
	$(TF) -chdir=bootstrap apply

init:                            ## terraform init for the current ENV.
	$(TF) -chdir=$(ENV_DIR) init

plan:                            ## terraform plan for the current ENV.
	$(TF) -chdir=$(ENV_DIR) plan -out=tfplan

apply:                           ## terraform apply the most recent plan.
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
