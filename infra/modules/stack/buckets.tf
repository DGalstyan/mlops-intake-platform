# infra/modules/stack/buckets.tf — the four data buckets the intake pipeline
# uses. Names are suffixed with the account id because S3 bucket names must
# be globally unique across all of AWS, not just within this account; every
# other resource in this stack uses the bare intake-<component>-<env>
# convention (see docs/decisions.md for this one documented exception). The
# convention name is still applied as the `Name` tag.

module "raw_bucket" {
  source = "../s3_bucket"

  name        = "${local.name_prefix}raw-${var.environment}-${local.account_id}"
  kms_key_arn = module.kms.key_arn

  # Raw uploads exist only to feed the pipeline; short retention.
  expiration_days = var.raw_expiration_days

  tags = merge(local.common_tags, {
    component = "raw"
    Name      = "${local.name_prefix}raw-${var.environment}"
  })
}

module "processed_bucket" {
  source = "../s3_bucket"

  name        = "${local.name_prefix}processed-${var.environment}-${local.account_id}"
  kms_key_arn = module.kms.key_arn

  # No expiration: OCR'd/normalized output feeds training and audit. Just
  # cost-optimize with a storage-class transition.
  transitions = [
    { days = var.processed_transition_days, storage_class = "STANDARD_IA" },
  ]

  tags = merge(local.common_tags, {
    component = "processed"
    Name      = "${local.name_prefix}processed-${var.environment}"
  })
}

module "artifacts_bucket" {
  source = "../s3_bucket"

  name        = "${local.name_prefix}artifacts-${var.environment}-${local.account_id}"
  kms_key_arn = module.kms.key_arn

  # No expiration: model.tar.gz / evaluation reports are lineage. Transition
  # down through IA then Glacier to keep cost bounded on old versions.
  transitions = [
    { days = var.artifacts_transition_ia_days, storage_class = "STANDARD_IA" },
    { days = var.artifacts_transition_glacier_days, storage_class = "GLACIER" },
  ]

  tags = merge(local.common_tags, {
    component = "artifacts"
    Name      = "${local.name_prefix}artifacts-${var.environment}"
  })
}

module "data_capture_bucket" {
  source = "../s3_bucket"

  name        = "${local.name_prefix}data-capture-${var.environment}-${local.account_id}"
  kms_key_arn = module.kms.key_arn

  # Endpoint data-capture records expire after the monitoring/drift-baseline
  # retention window — see docs/decisions.md.
  expiration_days = var.data_capture_expiration_days

  tags = merge(local.common_tags, {
    component = "data-capture"
    Name      = "${local.name_prefix}data-capture-${var.environment}"
  })
}
