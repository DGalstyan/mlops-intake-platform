#!/usr/bin/env python3
"""Resolve the latest **Approved** model package version in a group.

M2's rule is "only deploy a registry version whose status is Approved". That rule
is enforced here rather than in Terraform, for two reasons:

- A Terraform data source that re-resolved "latest approved" on every plan would
  make the deployed version an invisible, moving input: someone approves a version
  in the console and the next unrelated `terraform apply` silently ships it. The
  version ARN should appear in a plan diff as a deliberate change.
- The refusal needs to be loud. A resolver that returns nothing when no version is
  approved lets a deploy proceed with an empty variable and fail confusingly much
  later; this one exits non-zero with the actual statuses it found.

Prints the ARN on stdout so it can be piped straight into a `-var` assignment.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

APPROVED = "Approved"


def select_latest_approved(packages: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Pick the highest-numbered Approved version.

    Ordering is by `ModelPackageVersion`, not by creation time: versions are
    monotonic within a group, whereas approval can happen out of order (someone
    approves v3 today and then v2 tomorrow after a review), and "latest approved"
    should mean the newest model, not the most recently clicked.
    """
    approved = [
        package
        for package in packages
        if package.get("ModelApprovalStatus") == APPROVED
    ]
    if not approved:
        statuses = sorted(
            {
                f"v{p.get('ModelPackageVersion')}={p.get('ModelApprovalStatus')}"
                for p in packages
            }
        )
        raise SystemExit(
            "refusing to deploy: no Approved version in this model package group.\n"
            f"  found: {statuses or ['<no versions at all>']}\n"
            "  Approve a version in the SageMaker Model Registry first. Approval is "
            "the human gate this pipeline is built around — it is what emits the "
            "EventBridge event that triggers the canary deploy."
        )
    return max(approved, key=lambda p: int(p["ModelPackageVersion"]))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-package-group", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full package summary rather than just the ARN.",
    )
    args = parser.parse_args(argv)

    import boto3

    client = boto3.client("sagemaker", region_name=args.region)
    paginator = client.get_paginator("list_model_packages")
    packages: list[dict[str, Any]] = []
    for page in paginator.paginate(
        ModelPackageGroupName=args.model_package_group,
        # Ascending then max() by version, rather than trusting a descending page
        # order — the API's ordering guarantees are about creation time, not
        # version number.
        SortBy="CreationTime",
        SortOrder="Ascending",
    ):
        packages.extend(page.get("ModelPackageSummaryList", []))

    selected = select_latest_approved(packages)

    if args.json:
        print(json.dumps(selected, indent=2, default=str))
    else:
        print(selected["ModelPackageArn"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
