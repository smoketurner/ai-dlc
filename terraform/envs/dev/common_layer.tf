################################################################################
# Shared Lambda layer for `common` + the runtime deps every platform Lambda
# uses. Carries:
#   - the `common` workspace package source (under python/common/)
#   - aws-lambda-powertools[tracer,parser,aws-sdk] (pulls aws-xray-sdk +
#     pydantic + boto3 — but only [tracer]'s xray pin is reproducible from
#     this; pydantic and boto3 are pinned explicitly below for determinism)
#   - boto3, pydantic (explicit pins so rebuilds are reproducible
#     regardless of how powertools' [aws-sdk] / [parser] extras resolve)
#   - httpx, pyjwt, uuid-utils
#
# Per-Lambda requirements.txt files are trimmed to Lambda-specific deps
# (bedrock-agentcore, pyyaml, …). The layer is attached to every Lambda
# the platform owns.
################################################################################

module "common_layer" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  create_function = false
  create_layer    = true

  layer_name               = "${var.project}-${var.env}-common"
  description              = "Shared ai-dlc Python runtime: common package + powertools + boto3."
  runtime                  = "python3.13"
  compatible_runtimes      = ["python3.13"]
  compatible_architectures = ["arm64"]

  source_path = [
    {
      path          = "${path.module}/../../../packages/common/src/common"
      prefix_in_zip = "python/common"
    },
    {
      path             = "${path.module}/../../../packages/common/layer"
      pip_requirements = true
      prefix_in_zip    = "python"
    },
  ]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.13:latest-arm64"
}
