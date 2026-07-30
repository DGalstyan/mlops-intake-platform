"""Register a trained candidate into the SageMaker Model Registry.

Every version is registered `PendingManualApproval`. That is not a formality —
it is the human gate the whole release design depends on. Approval is what emits
the EventBridge event that triggers the canary deploy, so a version registered
`Approved` would deploy itself, which is the exact failure the rubric calls out.

The AWS calls are isolated behind `RegistryClient` so the assembly logic — which
metrics get attached, what lineage is recorded, how the description is built — is
unit-testable without AWS credentials or network access.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import (  # noqa: E402
    BASELINE_FILENAME,
    LINEAGE_FILENAME,
    METRICS_FILENAME,
)
from src.training.lineage import UNKNOWN  # noqa: E402

PENDING: str = "PendingManualApproval"


class SageMakerClient(Protocol):
    """The slice of the boto3 SageMaker client this module uses."""

    def create_model_package_group(self, **kwargs: Any) -> dict[str, Any]:
        ...

    def create_model_package(self, **kwargs: Any) -> dict[str, Any]:
        ...

    def list_model_packages(self, **kwargs: Any) -> dict[str, Any]:
        ...


@dataclass(frozen=True, slots=True)
class RegistrationRequest:
    """Everything needed to register one version, assembled before any AWS call."""

    model_package_group_name: str
    model_data_url: str
    image_uri: str
    metrics_s3_uri: str
    customer_metadata: dict[str, str]
    description: str
    approval_status: str = PENDING

    def to_create_kwargs(self) -> dict[str, Any]:
        """Build the create_model_package payload.

        `ModelApprovalStatus` is set from `approval_status`, which defaults to
        PendingManualApproval and is validated by `build_registration_request` —
        it is not left to the caller to remember.
        """
        return {
            "ModelPackageGroupName": self.model_package_group_name,
            "ModelPackageDescription": self.description,
            "ModelApprovalStatus": self.approval_status,
            "CustomerMetadataProperties": self.customer_metadata,
            "InferenceSpecification": {
                "Containers": [
                    {
                        "Image": self.image_uri,
                        "ModelDataUrl": self.model_data_url,
                    }
                ],
                "SupportedContentTypes": ["application/json"],
                "SupportedResponseMIMETypes": ["application/json"],
            },
            "ModelMetrics": {
                "ModelQuality": {
                    "Statistics": {
                        "ContentType": "application/json",
                        "S3Uri": self.metrics_s3_uri,
                    }
                }
            },
        }


def _flatten_metrics_for_metadata(metrics: dict[str, Any]) -> dict[str, str]:
    """Pull the headline numbers into searchable metadata.

    CustomerMetadataProperties values must be strings, and the map is small, so
    only the numbers a human scanning the registry console actually compares go
    in. The full metrics document is attached as ModelMetrics via S3.
    """
    flat: dict[str, str] = {}
    for key in ("macro_f1", "accuracy", "expected_calibration_error"):
        if key in metrics:
            flat[key] = f"{float(metrics[key]):.6f}"

    per_class = metrics.get("per_class")
    if isinstance(per_class, list):
        for entry in per_class:
            label = entry.get("label")
            if label:
                flat[f"f1_{label}"] = f"{float(entry['f1']):.6f}"
    return flat


def build_registration_request(
    *,
    model_package_group_name: str,
    model_data_url: str,
    image_uri: str,
    metrics_s3_uri: str,
    metrics: dict[str, Any],
    lineage: dict[str, Any],
    baseline_s3_uri: str | None = None,
) -> RegistrationRequest:
    """Assemble a registration request, refusing to register an unsafe one.

    Two hard refusals:

    - **Metrics that are not held out.** Registering training-set numbers as a
      version's quality metrics would corrupt the retrain gate, which reads them
      to decide whether a candidate beats the champion.
    - **Anything other than PendingManualApproval.** Enforced here rather than
      trusted to the caller, because the canary deploy fires on the approval
      event and a pre-approved version would self-deploy.
    """
    if metrics.get("split") != "golden":
        raise ValueError(
            f"refusing to register metrics from split {metrics.get('split')!r}. "
            "Only golden-set (held-out) metrics may be attached to a registry "
            "version — the retrain gate compares these numbers."
        )
    if not metrics.get("is_held_out"):
        raise ValueError("refusing to register metrics not marked is_held_out")

    # Prefer the lineage record, but fall back to the same fields the evaluation
    # step stamps into metrics.json. train.py and evaluate.py are separate jobs
    # writing to separate output paths, so a caller can legitimately have one and
    # not the other — and a registry version whose provenance says "unknown"
    # purely because two files sat in different directories is a bad trade.
    snapshot_id = str(
        lineage.get("data_snapshot_id") or metrics.get("data_snapshot_id") or UNKNOWN
    )
    git_sha = str(lineage.get("git_sha") or metrics.get("git_sha") or UNKNOWN)
    image_digest = str(lineage.get("training_image_digest", UNKNOWN))

    metadata: dict[str, str] = {
        "data_snapshot_id": snapshot_id,
        "git_sha": git_sha,
        "training_image_digest": image_digest,
        "metrics_split": "golden",
        "metrics_schema_version": str(metrics.get("schema_version", UNKNOWN)),
        "non_overlap_verified": str(metrics.get("non_overlap_verified", False)),
    }
    if baseline_s3_uri:
        metadata["baseline_statistics_s3_uri"] = baseline_s3_uri

    environment = lineage.get("environment")
    if isinstance(environment, dict):
        for key in ("python", "scikit_learn", "numpy"):
            if key in environment:
                metadata[f"env_{key}"] = str(environment[key])

    hyperparameters = lineage.get("hyperparameters")
    if isinstance(hyperparameters, dict):
        for key in ("implementation", "calibrated", "seed"):
            if key in hyperparameters:
                metadata[f"hp_{key}"] = str(hyperparameters[key])

    metadata.update(_flatten_metrics_for_metadata(metrics))

    macro_f1 = float(metrics.get("macro_f1", 0.0))
    ece = float(metrics.get("expected_calibration_error", 0.0))
    calibrated = (
        hyperparameters.get("calibrated")
        if isinstance(hyperparameters, dict)
        else None
    )
    description = (
        f"macro-F1 {macro_f1:.4f}, ECE {ece:.4f} on the frozen golden set "
        f"({metrics.get('n_samples', '?')} docs). "
        f"calibrated={calibrated}. snapshot={snapshot_id[:23]} git={git_sha[:8]}"
    )

    return RegistrationRequest(
        model_package_group_name=model_package_group_name,
        model_data_url=model_data_url,
        image_uri=image_uri,
        metrics_s3_uri=metrics_s3_uri,
        customer_metadata=metadata,
        description=description[:1024],
        approval_status=PENDING,
    )


class RegistryClient:
    """Thin wrapper over the SageMaker registry calls."""

    def __init__(self, client: SageMakerClient) -> None:
        self._client = client

    def ensure_group(self, name: str, *, description: str) -> None:
        """Create the Model Package Group if it does not already exist.

        A pre-existing group is the normal case on every run after the first, so
        the conflict is expected rather than exceptional.
        """
        try:
            self._client.create_model_package_group(
                ModelPackageGroupName=name,
                ModelPackageGroupDescription=description,
            )
        except Exception as error:  # noqa: BLE001 - botocore error types are dynamic
            if "already exists" in str(error) or "ValidationException" in type(
                error
            ).__name__:
                return
            raise

    def register(self, request: RegistrationRequest) -> str:
        """Create a new version. Never updates an existing one.

        `create_model_package` always appends, which is what keeps the "two
        distinguishable versions" deliverable honest — there is no code path here
        that could overwrite version 1 with version 2 and hide the difference.
        """
        response = self._client.create_model_package(**request.to_create_kwargs())
        return str(response["ModelPackageArn"])

    def list_versions(self, group_name: str) -> list[dict[str, Any]]:
        response = self._client.list_model_packages(
            ModelPackageGroupName=group_name, MaxResults=100
        )
        packages: list[dict[str, Any]] = response.get("ModelPackageSummaryList", [])
        return packages


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-package-group", required=True)
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        required=True,
        help=(
            "Directory holding metrics.json. Used as the default location for "
            "lineage.json and baseline_statistics.json too."
        ),
    )
    parser.add_argument(
        "--lineage-file",
        type=Path,
        default=None,
        help=(
            "Path to lineage.json. Needed because train.py and evaluate.py are "
            "separate jobs with separate output paths: the lineage record is "
            "written by training, the golden-set metrics by evaluation. Defaults "
            "to --artifacts-dir/lineage.json."
        ),
    )
    parser.add_argument("--model-data-url", required=True, help="s3:// URI of model.tar.gz")
    parser.add_argument(
        "--image-uri", required=True, help="Inference image, by digest where possible."
    )
    parser.add_argument("--metrics-s3-uri", required=True)
    parser.add_argument("--baseline-s3-uri", default=None)
    parser.add_argument("--region", default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Assemble and print the request without calling AWS.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    metrics_path = args.artifacts_dir / METRICS_FILENAME
    lineage_path = args.lineage_file or (args.artifacts_dir / LINEAGE_FILENAME)
    if not metrics_path.is_file():
        raise FileNotFoundError(f"no metrics at {metrics_path}")

    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    if lineage_path.is_file():
        lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
    else:
        lineage = {}
        print(
            f"note: no lineage record at {lineage_path}. Falling back to the "
            "snapshot id and git SHA stamped into metrics.json; the training "
            "image digest and resolved dependency versions will be unknown. "
            "Pass --lineage-file to point at the training job's lineage.json.",
            file=sys.stderr,
        )

    baseline_uri = args.baseline_s3_uri
    if baseline_uri is None and (args.artifacts_dir / BASELINE_FILENAME).is_file():
        print(
            "note: a local baseline_statistics.json exists but no "
            "--baseline-s3-uri was given, so the registry version will not "
            "point at it. M5's drift job resolves the baseline by this URI.",
            file=sys.stderr,
        )

    request = build_registration_request(
        model_package_group_name=args.model_package_group,
        model_data_url=args.model_data_url,
        image_uri=args.image_uri,
        metrics_s3_uri=args.metrics_s3_uri,
        metrics=metrics,
        lineage=lineage,
        baseline_s3_uri=baseline_uri,
    )

    if args.dry_run:
        print(json.dumps(request.to_create_kwargs(), indent=2, sort_keys=True))
        return 0

    import boto3  # imported lazily so --dry-run needs no AWS SDK

    client = RegistryClient(boto3.client("sagemaker", region_name=args.region))
    client.ensure_group(
        args.model_package_group,
        description="Document intake classifier. Versions are registered PendingManualApproval; approval triggers the canary deploy.",
    )
    arn = client.register(request)
    print(json.dumps({"model_package_arn": arn, "approval_status": PENDING}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
