#!/usr/bin/env python3
"""Render the extraction prompts from schemas/ and write them to DynamoDB.

Run after `terraform apply` and again whenever a schema changes. This is the step
that makes the extraction prompt *data*: the state machine reads prompts from
DynamoDB with a direct GetItem, so adding a field or a document class is one JSON
edit plus a re-seed, with no code, ASL or Terraform change.

Idempotent — a plain PutItem per class, so re-running after a schema edit updates in
place. The template_version travels with each item so a change in extraction quality
can be attributed to a prompt revision rather than blamed on the model or the data.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.pipeline.prompts import render_all  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True, help="Prompts table name.")
    parser.add_argument("--region", default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the items without writing. Needs no AWS credentials.",
    )
    args = parser.parse_args(argv)

    rendered = render_all()
    items = [prompt.to_item() for prompt in rendered.values()]

    if args.dry_run:
        print(json.dumps(items, indent=2)[:4000])
        print(f"\n({len(items)} items; use --table without --dry-run to write)")
        return 0

    import boto3

    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
    for item in items:
        table.put_item(Item=item)
        print(
            f"seeded {item['document_class']}: "
            f"{len(item['prompt'])} prompt chars, "
            f"{len(item['required_fields'])} required fields, "
            f"template {item['template_version']}"
        )
    print(f"\n{len(items)} prompts written to {args.table}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
