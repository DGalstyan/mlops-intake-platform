"""Tests for the release-safety scripts.

These cover the two places where a mistake ships a bad model:

- `resolve_approved_model` must refuse to return anything that is not Approved.
  Approval is the human gate the whole release design rests on; a resolver that
  fell back to "most recent version" would silently bypass it.
- `smoke_test.validate_response` must reject a response whose contract has
  drifted. It runs as a release gate, so a permissive validator would let a
  breaking change through to M3, M4 and M5 — none of which would notice until
  runtime.
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts.resolve_approved_model import select_latest_approved
from scripts.smoke_test import validate_response


def package(version: int, status: str) -> dict[str, Any]:
    return {
        "ModelPackageVersion": version,
        "ModelApprovalStatus": status,
        "ModelPackageArn": f"arn:aws:sagemaker:us-east-1:000000000000:model-package/g/{version}",
    }


class TestResolveApproved:
    def test_picks_the_highest_approved_version(self) -> None:
        selected = select_latest_approved(
            [package(1, "Approved"), package(3, "Approved"), package(2, "Approved")]
        )
        assert selected["ModelPackageVersion"] == 3

    def test_ignores_pending_versions_even_when_newer(self) -> None:
        """The core rule: a newer unapproved version must not win.

        This is what stops an unreviewed model reaching production simply by being
        the latest thing registered.
        """
        selected = select_latest_approved(
            [package(1, "Approved"), package(2, "PendingManualApproval")]
        )
        assert selected["ModelPackageVersion"] == 1

    def test_ignores_rejected_versions(self) -> None:
        selected = select_latest_approved(
            [package(1, "Approved"), package(2, "Rejected")]
        )
        assert selected["ModelPackageVersion"] == 1

    def test_orders_by_version_not_approval_recency(self) -> None:
        """Approval can happen out of order; "latest approved" means newest model."""
        selected = select_latest_approved(
            [package(5, "Approved"), package(2, "Approved")]
        )
        assert selected["ModelPackageVersion"] == 5

    def test_refuses_when_nothing_is_approved(self) -> None:
        with pytest.raises(SystemExit, match="no Approved version"):
            select_latest_approved(
                [package(1, "PendingManualApproval"), package(2, "Rejected")]
            )

    def test_refuses_on_an_empty_group(self) -> None:
        with pytest.raises(SystemExit, match="no Approved version"):
            select_latest_approved([])

    def test_error_names_the_statuses_it_found(self) -> None:
        """The refusal must be diagnosable without a console visit."""
        with pytest.raises(SystemExit) as error:
            select_latest_approved([package(1, "PendingManualApproval")])
        assert "PendingManualApproval" in str(error.value)


def good_response() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "model_version": "v1",
        "predictions": [
            {
                "predicted_class": "invoice",
                "confidence": 0.9,
                "class_probabilities": {
                    "invoice": 0.9,
                    "medical_report": 0.04,
                    "id_document": 0.03,
                    "correspondence": 0.03,
                },
                "auto_approve_eligible": True,
                "confidence_threshold": 0.80,
            }
        ],
    }


class TestSmokeValidation:
    def test_accepts_a_valid_response(self) -> None:
        assert validate_response(good_response(), label="t") == []

    def test_rejects_a_renamed_contract_key(self) -> None:
        """The regression the release gate exists to catch.

        A rename returns 200 and looks perfect to CloudWatch, then breaks M3
        routing, M4 metrics and M5 drift parsing at runtime.
        """
        payload = good_response()
        payload["predictions"][0]["score"] = payload["predictions"][0].pop("confidence")
        failures = validate_response(payload, label="t")
        assert any("missing contract keys" in f for f in failures)

    def test_rejects_a_missing_class_in_the_distribution(self) -> None:
        payload = good_response()
        del payload["predictions"][0]["class_probabilities"]["id_document"]
        failures = validate_response(payload, label="t")
        assert any("omits" in f for f in failures)

    def test_rejects_probabilities_that_do_not_sum_to_one(self) -> None:
        payload = good_response()
        payload["predictions"][0]["class_probabilities"]["invoice"] = 0.5
        failures = validate_response(payload, label="t")
        assert any("sum to" in f for f in failures)

    def test_rejects_confidence_disagreeing_with_the_distribution(self) -> None:
        payload = good_response()
        payload["predictions"][0]["confidence"] = 0.55
        failures = validate_response(payload, label="t")
        assert any("does not equal" in f for f in failures)

    def test_rejects_a_stale_threshold(self) -> None:
        """Catches a deployed image built against an older config."""
        payload = good_response()
        payload["predictions"][0]["confidence_threshold"] = 0.5
        payload["predictions"][0]["auto_approve_eligible"] = True
        failures = validate_response(payload, label="t")
        assert any("stale" in f for f in failures)

    def test_rejects_an_inconsistent_auto_approve_flag(self) -> None:
        payload = good_response()
        payload["predictions"][0]["auto_approve_eligible"] = False
        failures = validate_response(payload, label="t")
        assert any("disagrees" in f for f in failures)

    def test_rejects_an_out_of_range_confidence(self) -> None:
        payload = good_response()
        payload["predictions"][0]["confidence"] = 1.7
        payload["predictions"][0]["class_probabilities"]["invoice"] = 1.7
        failures = validate_response(payload, label="t")
        assert any("not a probability" in f for f in failures)

    def test_rejects_a_missing_envelope_key(self) -> None:
        payload = good_response()
        del payload["schema_version"]
        failures = validate_response(payload, label="t")
        assert any("missing top-level keys" in f for f in failures)

    def test_rejects_empty_predictions(self) -> None:
        payload = good_response()
        payload["predictions"] = []
        failures = validate_response(payload, label="t")
        assert any("missing or empty" in f for f in failures)

    def test_flags_unexpected_extra_keys(self) -> None:
        """Additions are safe for consumers but must be a deliberate version bump."""
        payload = good_response()
        payload["predictions"][0]["debug_internal_state"] = {"x": 1}
        failures = validate_response(payload, label="t")
        assert any("unexpected keys" in f for f in failures)
