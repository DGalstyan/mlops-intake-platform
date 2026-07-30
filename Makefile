.DEFAULT_GOAL := help

# Environment selector for every target below. Usage: `make plan ENV=staging`.
ENV    ?= dev
REGION ?= us-east-1

BOOTSTRAP_DIR := infra/bootstrap
ENV_DIR       := infra/envs/$(ENV)

ifneq ($(filter-out dev staging,$(ENV)),)
$(error ENV must be "dev" or "staging", got "$(ENV)")
endif

.PHONY: help bootstrap destroy-bootstrap init fmt fmt-check validate validate-all \
        measure-throughput docker-build docker-smoke resolve-approved smoke-test \
        plan apply destroy venv test typecheck data train evaluate two-versions

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
	@echo ""
	@echo "  make venv                  Create .venv and install pinned dependencies."
	@echo "  make test                  Run the pytest suite."
	@echo "  make typecheck             Run mypy in strict mode."
	@echo "  make data                  Generate the synthetic dataset + snapshot id."
	@echo "  make train                 Train, writing model + baseline + lineage."
	@echo "  make evaluate              Score on the frozen golden set (the gate's numbers)."
	@echo "  make two-versions          Produce the two distinguishable M1 registry versions."

bootstrap:
	cd $(BOOTSTRAP_DIR) && terraform init
	cd $(BOOTSTRAP_DIR) && terraform apply -var="region=$(REGION)"

destroy-bootstrap:
	cd $(BOOTSTRAP_DIR) && terraform destroy -var="region=$(REGION)"

# The state bucket name is DERIVED from (project, account), not read from the
# bootstrap root's state. That root keeps local state, which is gitignored, so
# a `terraform output` lookup only works on the one machine that ran
# `make bootstrap` — it fails on a fresh clone and on every CI runner, which
# would make the M6 deploy path impossible. Deriving it needs nothing but
# credentials. Override STATE_BUCKET explicitly if you renamed it.
# Must stay in step with local.state_bucket_name in infra/bootstrap/main.tf
# and infra/modules/stack/main.tf.
PROJECT ?= intake
STATE_BUCKET ?= $(PROJECT)-tfstate-$(shell aws sts get-caller-identity --query Account --output text 2>/dev/null)

init:
	@if ! echo "$(STATE_BUCKET)" | grep -qE '^$(PROJECT)-tfstate-[0-9]{12}$$'; then \
		echo "error: could not derive the state bucket name (got '$(STATE_BUCKET)')."; \
		echo "       Check your AWS credentials — 'aws sts get-caller-identity' must work."; \
		echo "       Or pass it explicitly: make $(MAKECMDGOALS) STATE_BUCKET=my-bucket"; \
		exit 1; \
	fi
	cd $(ENV_DIR) && terraform init \
		-backend-config="bucket=$(STATE_BUCKET)" \
		-backend-config="region=$(REGION)" \
		-reconfigure

fmt:
	terraform fmt -recursive

# Non-mutating counterpart of `fmt`, for CI: fails instead of rewriting files.
fmt-check:
	terraform fmt -check -recursive

# Deliberately uses `-backend=false` rather than depending on `init`: validation
# must run with no AWS credentials and no bootstrapped state bucket, so that it
# works on a fresh clone and in the M6 pull-request workflow. Covers the
# bootstrap root too, which `init`-based targets never touch.
validate:
	cd $(BOOTSTRAP_DIR) && terraform init -backend=false -input=false >/dev/null && terraform validate
	cd $(ENV_DIR) && terraform init -backend=false -input=false >/dev/null && terraform validate

# `validate` only covers the selected ENV, so it never touches staging. CI runs
# this one.
validate-all:
	$(MAKE) validate ENV=dev
	$(MAKE) validate ENV=staging

plan: init
	cd $(ENV_DIR) && terraform plan -var-file="$(ENV).tfvars"

apply: init
	cd $(ENV_DIR) && terraform apply -var-file="$(ENV).tfvars"

# `make destroy` tears down one environment's stack (KMS, ECR, buckets, IAM
# roles). Two things deliberately survive it:
#
#   1. The shared state backend and the GitHub OIDC provider — bootstrap
#      resources shared across environments. Tear them down with
#      `make destroy-bootstrap` AFTER every environment is destroyed.
#   2. The KMS key, which enters PendingDeletion for 7 days (AWS's minimum;
#      zero is not permitted) and keeps billing ~$0.23 prorated per
#      environment. The alias is deleted immediately, so the key appears in
#      the console without a friendly name. Nothing can shorten this.
#
# Everything else is gone when this returns. See the README teardown section.
destroy: init
	cd $(ENV_DIR) && terraform destroy -var-file="$(ENV).tfvars"
	@echo ""
	@echo "NOTE: the KMS key for ENV=$(ENV) is now PendingDeletion for 7 days"
	@echo "      and still bills (~\$$0.23 prorated). This is AWS's floor."
	@echo "      Run 'make destroy-bootstrap' once ALL environments are down."

# --- Python (M1+) ----------------------------------------------------------
VENV   := .venv
PY     := $(VENV)/bin/python
DATA_DIR ?= data/snapshot
ARTIFACTS_DIR ?= artifacts

$(VENV)/bin/python:
	python3.12 -m venv $(VENV)
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -r requirements-dev.txt

venv: $(VENV)/bin/python

test: venv
	$(PY) -m pytest

typecheck: venv
	$(PY) -m mypy

# Regenerate the dataset. Deterministic: same seed, same bytes, same snapshot id.
data: venv
	$(PY) -m src.data.generate --output-dir $(DATA_DIR)

train: venv
	$(PY) -m src.training.train \
		--train-dir $(DATA_DIR) \
		--model-dir $(ARTIFACTS_DIR)/model \
		--output-dir $(ARTIFACTS_DIR)/output

# The numbers that gate a release: held-out, on the frozen golden set.
evaluate: venv
	$(PY) -m src.training.evaluate \
		--model-dir $(ARTIFACTS_DIR)/model \
		--data-dir $(DATA_DIR) \
		--output-dir $(ARTIFACTS_DIR)/evaluation

# Produce the two distinguishable registry versions M1 is graded on. The second
# run disables probability calibration, so the versions differ in ECE — the
# metric the confidence gate actually depends on — rather than by random noise.
two-versions: venv data
	$(PY) -m src.training.train --train-dir $(DATA_DIR) \
		--model-dir $(ARTIFACTS_DIR)/v1/model --output-dir $(ARTIFACTS_DIR)/v1/output
	$(PY) -m src.training.evaluate --model-dir $(ARTIFACTS_DIR)/v1/model \
		--data-dir $(DATA_DIR) --output-dir $(ARTIFACTS_DIR)/v1/evaluation
	$(PY) -m src.training.train --train-dir $(DATA_DIR) --no-calibration \
		--model-dir $(ARTIFACTS_DIR)/v2/model --output-dir $(ARTIFACTS_DIR)/v2/output
	$(PY) -m src.training.evaluate --model-dir $(ARTIFACTS_DIR)/v2/model \
		--data-dir $(DATA_DIR) --output-dir $(ARTIFACTS_DIR)/v2/evaluation \
		--champion-metrics $(ARTIFACTS_DIR)/v1/evaluation/metrics.json

# --- M2 deployment ---------------------------------------------------------
ENDPOINT_NAME ?= intake-classifier-$(ENV)
MODEL_PACKAGE_GROUP ?= intake-classifier-$(ENV)

# Load measurement that justifies the autoscaling target and latency alarm.
# Needs no AWS.
measure-throughput: venv
	$(PY) scripts/measure_throughput.py --requests 1200 --warmup 100 \
		--output evidence/m2/throughput.json

# Build the inference image. Tagged by content digest, not "latest": the digest is
# what gets recorded as model lineage, and a mutable tag would make that lineage a
# lie. ECR has immutable tags enabled, so a re-push under an existing tag fails.
IMAGE_TAG ?= $(shell git rev-parse --short HEAD)
docker-build:
	docker build -f src/inference/Dockerfile -t intake-inference:$(IMAGE_TAG) .

# Verify the container's own /ping and /invocations contract before it ever
# reaches SageMaker.
docker-smoke: docker-build
	./scripts/container_smoke.sh intake-inference:$(IMAGE_TAG)

# Resolve the version to deploy. Refuses anything not Approved — approval is the
# human gate the release design is built around.
resolve-approved: venv
	$(PY) scripts/resolve_approved_model.py --model-package-group $(MODEL_PACKAGE_GROUP) --region $(REGION)

# Post-deploy release gate. Non-zero exit means reject the release.
smoke-test: venv
	$(PY) scripts/smoke_test.py --endpoint-name $(ENDPOINT_NAME) --region $(REGION) \
		--require-confidence-spread
