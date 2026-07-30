# infra/modules/ecr — one repository for the inference/training container
# image. Immutable tags (no overwriting a tag once pushed — every deploy is
# traceable to one image digest) + scan on push.

resource "aws_ecr_repository" "this" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_key_arn
  }

  # Scratch/take-home repo: allow `make destroy` to remove it even if images
  # remain, rather than requiring a manual `aws ecr batch-delete-image` first.
  force_delete = true

  tags = var.tags
}

# Expire untagged images (left behind by immutable-tag re-pushes under a new
# tag, or by failed pushes) so the repo doesn't grow unbounded between runs.
resource "aws_ecr_lifecycle_policy" "untagged_expiry" {
  repository = aws_ecr_repository.this.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after ${var.untagged_image_expiry_days} days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = var.untagged_image_expiry_days
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}
