"""Lineage capture — what a registered model version can be traced back to.

The registry needs to answer "how did this model come to exist?" without anyone
having to remember. That means three identifiers travelling with every artifact:

- **data snapshot id** — content hash of the exact training bytes.
- **git SHA** — the code that produced it.
- **training image digest** — the container it ran in, by immutable digest rather
  than by tag, because a tag can be re-pointed at a different image.

All three are read from the environment SageMaker provides, with an explicit
`unknown` sentinel rather than a silent default. A lineage field that quietly
says "main" or "latest" when it does not know is worse than one that says it does
not know, because only the second is detectable in a review.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

UNKNOWN: Final[str] = "unknown"


@dataclass(frozen=True, slots=True)
class Lineage:
    """Provenance for one training run."""

    data_snapshot_id: str
    git_sha: str
    training_image_digest: str
    environment: dict[str, str]
    hyperparameters: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2)

    def is_complete(self) -> bool:
        """True when nothing is `unknown`.

        The registration step warns on an incomplete lineage rather than
        refusing: a local smoke run legitimately has no image digest, and
        blocking it would push people towards faking the value.
        """
        return UNKNOWN not in {
            self.data_snapshot_id,
            self.git_sha,
            self.training_image_digest,
        }

    def missing_fields(self) -> list[str]:
        return [
            name
            for name, value in (
                ("data_snapshot_id", self.data_snapshot_id),
                ("git_sha", self.git_sha),
                ("training_image_digest", self.training_image_digest),
            )
            if value == UNKNOWN
        ]


def resolve_git_sha() -> str:
    """The commit that produced this run.

    Prefers an explicitly injected value, because a SageMaker container has no
    git history — CI passes GIT_SHA as a hyperparameter or environment variable.
    Falls back to asking git, which is what happens on a developer's laptop.
    """
    for variable in ("GIT_SHA", "GITHUB_SHA"):
        value = os.environ.get(variable)
        if value:
            return value.strip()

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    if result.returncode != 0:
        return UNKNOWN
    return result.stdout.strip() or UNKNOWN


def resolve_training_image_digest() -> str:
    """The immutable digest of the container this ran in.

    SageMaker exposes the training image URI in the environment. When it carries
    an `@sha256:` digest we keep it as-is; a tag-only URI is recorded verbatim
    but is a weaker claim, and the registration step surfaces that.
    """
    for variable in ("SAGEMAKER_TRAINING_IMAGE", "TRAINING_IMAGE_URI", "IMAGE_URI"):
        value = os.environ.get(variable)
        if value:
            return value.strip()
    return UNKNOWN


def read_snapshot_id(data_dir: Path, *, manifest_filename: str) -> str:
    """Read the snapshot id from the manifest the generator wrote alongside the data."""
    manifest_path = data_dir / manifest_filename
    if not manifest_path.is_file():
        return UNKNOWN
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return UNKNOWN
    snapshot_id = payload.get("snapshot_id")
    return str(snapshot_id) if snapshot_id else UNKNOWN


def collect(
    *,
    data_snapshot_id: str,
    environment: dict[str, str],
    hyperparameters: dict[str, Any],
) -> Lineage:
    return Lineage(
        data_snapshot_id=data_snapshot_id,
        git_sha=resolve_git_sha(),
        training_image_digest=resolve_training_image_digest(),
        environment=environment,
        hyperparameters=hyperparameters,
    )
