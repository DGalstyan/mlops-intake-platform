.DEFAULT_GOAL := help

# Environment selector for every target below. Usage: `make plan ENV=staging`.
ENV    ?= dev
REGION ?= us-east-1

BOOTSTRAP_DIR := infra/bootstrap
ENV_DIR       := infra/envs/$(ENV)

ifneq ($(filter-out dev staging,$(ENV)),)
$(error ENV must be "dev" or "staging", got "$(ENV)")
endif

.PHONY: help bootstrap destroy-bootstrap init fmt validate plan apply destroy test

help:
	@echo "Targets:"
	@echo "  make bootstrap             Create the remote state backend (S3 + native lock). Run once, local state."
	@echo "  make destroy-bootstrap     Tear down the state backend. Run last, after every env has been destroyed."
	@echo "  make init ENV=dev|staging  terraform init for one environment, backend config from bootstrap outputs."
	@echo "  make fmt                   terraform fmt -recursive across infra/."
	@echo "  make validate ENV=dev      terraform validate for one environment root."
	@echo "  make plan ENV=dev          terraform plan for one environment."
	@echo "  make apply ENV=dev         terraform apply for one environment."
	@echo "  make destroy ENV=dev       terraform destroy for one environment."

bootstrap:
	cd $(BOOTSTRAP_DIR) && terraform init
	cd $(BOOTSTRAP_DIR) && terraform apply -var="region=$(REGION)"

destroy-bootstrap:
	cd $(BOOTSTRAP_DIR) && terraform destroy -var="region=$(REGION)"

# Reads the state bucket name straight from the bootstrap root's own local
# state/output — no manual copy-pasting of the bucket name, no committed
# backend.hcl. Requires `make bootstrap` to have been applied first.
init:
	$(eval STATE_BUCKET := $(shell cd $(BOOTSTRAP_DIR) && terraform output -raw state_bucket_name))
	@if [ -z "$(STATE_BUCKET)" ]; then \
		echo "error: could not read state_bucket_name from $(BOOTSTRAP_DIR) — run 'make bootstrap' first."; \
		exit 1; \
	fi
	cd $(ENV_DIR) && terraform init \
		-backend-config="bucket=$(STATE_BUCKET)" \
		-backend-config="region=$(REGION)" \
		-reconfigure

fmt:
	terraform fmt -recursive

# Deliberately uses `-backend=false` rather than depending on `init`: validation
# must run with no AWS credentials and no bootstrapped state bucket, so that it
# works on a fresh clone and in the M6 pull-request workflow. Covers the
# bootstrap root too, which `init`-based targets never touch.
validate:
	cd $(BOOTSTRAP_DIR) && terraform init -backend=false -input=false >/dev/null && terraform validate
	cd $(ENV_DIR) && terraform init -backend=false -input=false >/dev/null && terraform validate

plan: init
	cd $(ENV_DIR) && terraform plan -var-file="$(ENV).tfvars"

apply: init
	cd $(ENV_DIR) && terraform apply -var-file="$(ENV).tfvars"

# `make destroy` tears down one environment's stack (KMS, ECR, buckets, IAM
# roles). It does NOT tear down the shared state backend or the GitHub OIDC
# provider — those are bootstrap resources shared across environments and
# are torn down separately with `make destroy-bootstrap` once every
# environment has been destroyed. See docs/decisions.md.
destroy: init
	cd $(ENV_DIR) && terraform destroy -var-file="$(ENV).tfvars"

test:
	@echo "No application tests exist yet (see tasks/M1+)."
