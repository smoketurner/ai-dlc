# Bootstrap

One-time Terraform module that creates the **S3 bucket + DynamoDB lock table** used by every environment's state. Run it manually with a **local** backend, then migrate the bootstrap's own state into the bucket it just provisioned.

## First-run

```bash
cd terraform/bootstrap
terraform init
terraform apply
# Note the `backend_hcl` output — paste the values into envs/<env>/backend.tf.
```

After apply, optionally migrate this module's state into the new bucket:

```bash
# In bootstrap/, add the s3 backend block to versions.tf, then:
terraform init -migrate-state
```

## Re-running

This module is idempotent — re-applying does nothing unless `var.region` or `var.project` change. The S3 bucket has `prevent_destroy = true` to avoid accidentally orphaning every environment's state.
