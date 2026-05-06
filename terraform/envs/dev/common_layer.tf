################################################################################
# Shared Lambda layer for the `common` workspace package.
#
# Every platform Lambda that imports `from common.*` (entry_adapter,
# triage_dispatcher, event_projector, telemetry) needs the package on
# sys.path. The layer ships only the Python source — transitive runtime
# deps stay in each Lambda's requirements.txt so per-function bundles
# remain narrow.
#
# Layout in the zip is `python/common/...`, the AWS-mandated prefix that
# puts the directory on sys.path at cold start.
################################################################################

module "common_layer" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 8.0"

  create_function = false
  create_layer    = true

  layer_name               = "${var.project}-${var.env}-common"
  description              = "Shared ai-dlc Python utilities (events, ids, telemetry, AWS adapters)."
  compatible_runtimes      = ["python3.13"]
  compatible_architectures = ["arm64"]

  source_path = [{
    path          = "${path.module}/../../../packages/common/src/common"
    prefix_in_zip = "python/common"
  }]
  build_in_docker = true
  docker_image    = "public.ecr.aws/sam/build-python3.13:latest-arm64"
}
