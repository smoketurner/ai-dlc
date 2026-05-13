# Team-shared dev environment config.
#
# Auto-loaded by both local ``terraform apply`` and CI. Anything that
# differs between the two (or carries secrets) stays elsewhere:
#
#   * ``aws_profile``, ``github_owner``, ``github_repo`` — provided by
#     the CI workflow as ``TF_VAR_*`` env vars (derived from the runner
#     context, or set to ``""`` to fall through to OIDC credentials).
#   * Personal overrides — uncommitted ``terraform.tfvars`` (gitignored).
#   * Secret values — live in Secrets Manager; we reference the
#     **secret name** here, not its value.
#
# Terraform precedence (highest first): ``-var`` flags > ``*.auto.tfvars``
# > ``terraform.tfvars`` > ``TF_VAR_*`` env vars > defaults. So a CI run
# can still override an entry here via ``-var key=value`` on the CLI
# (rarely needed).

github_owner           = "smoketurner"
github_repo            = "ai-dlc"
github_app_secret_name = "ai-dlc/github-app"
github_bot_login       = "aidlc-bot"

# ``alert_emails`` is set via ``TF_VAR_alert_emails`` in CI (sourced
# from the ``AIDLC_ALERT_EMAILS`` repo variable) so personal email
# addresses stay out of git. Locally, add your own entry to the
# gitignored ``terraform.tfvars``.

# Bedrock per-model quota alarms (warn/high/critical at 50/80/95% of
# the live Service Quotas value). Codes resolved from us-east-1 via
# ``aws service-quotas list-service-quotas --service-code bedrock``.
# Quota meaning per code:
#   tpm = Cross-region model inference tokens per minute
#   rpm = Cross-region model inference requests per minute
#   tpd = Model invocation max tokens per day (doubled for cross-region calls)
bedrock_quota_codes = {
  opus_4_6 = {
    tpm = "L-0AD9BBE8"
    rpm = "L-11DFF789"
    tpd = "L-82CD9B28"
  }
  sonnet_4_6 = {
    tpm = "L-15B8E632"
    rpm = "L-00FF3314"
    tpd = "L-B29C9321"
  }
  haiku_4_5 = {
    tpm = "L-58BE175A"
    rpm = "L-CCA5DF70"
    tpd = "L-6120CF2D"
  }
}
