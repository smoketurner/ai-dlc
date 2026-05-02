# Bootstrap

One-time Terraform module that adopts the **S3 bucket** used by every environment's state. State is encrypted at rest with SSE-S3 (AES256). Locking is handled by S3 native lockfile (`use_lockfile = true`) — no DynamoDB lock table is required.

The bucket name is fixed at `terraform-state-<account-id>-<region>-an` (created out-of-band; adopted via a one-time `import` block on first apply).

## First-run

```bash
cd terraform/bootstrap
terraform init
terraform apply
# Note the `backend_hcl` output — paste the values into envs/<env>/backend.tf.
```

## State migration (optional)

After apply, optionally migrate this module's own state into the new bucket:

```bash
# In bootstrap/, add the s3 backend block to versions.tf, then:
terraform init -migrate-state
```

## Re-running

Idempotent — re-applying is a no-op unless config drifts. The bucket has `prevent_destroy = true` so `terraform destroy` won't orphan every environment's state.

## What's intentionally not here

- **No lifecycle expiration on noncurrent versions.** State files are precious — old versions are the recovery path when a bad apply is detected. We never expire them.
- **No DynamoDB lock table.** S3 native lockfiles (`use_lockfile = true`) cover the locking story.
