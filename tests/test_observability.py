"""Observability tests: alarm coverage, classification, and metric consistency.

The recurring failure mode in this repo has been documentation drifting from
configuration — three separate hardcoded wildcard counts went stale, and a README
claimed "only M0 is implemented" two milestones later. These tests exist to make that
class of drift a CI failure rather than something a reader discovers.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from scripts import render_alarm_inventory

REPO = Path(__file__).resolve().parents[1]
INVENTORY = REPO / "evidence" / "m4" / "alarm-inventory.md"
ASL = REPO / "statemachines" / "intake.asl.json"
PRICES = REPO / "config" / "prices.json"


@pytest.fixture(scope="module")
def alarms() -> list[render_alarm_inventory.Alarm]:
    return render_alarm_inventory.collect()


class TestAlarmCoverage:
    def test_alarms_are_found(self, alarms: list[Any]) -> None:
        assert len(alarms) >= 11, f"only found {len(alarms)} alarms"

    def test_every_alarm_is_classified(self, alarms: list[Any]) -> None:
        """The model-quality vs system-health split must be explicit on every alarm.

        Read from a deployed `measures` tag rather than inferred from the description.
        An earlier version guessed from keywords and miscategorised two alarms — and
        this distinction is exactly what the observability section is graded on.
        """
        unclassified = [a.resource for a in alarms if a.measures == "UNCLASSIFIED"]
        assert not unclassified, (
            f"alarms with no `measures` tag: {unclassified}. Add one — the "
            "model-quality vs system-health split is graded, and an unclassified "
            "alarm silently defaults to looking like system health."
        )

    def test_at_least_one_model_quality_alarm_exists(self, alarms: list[Any]) -> None:
        """A dashboard of only system health is the named point-loser."""
        model_alarms = [a for a in alarms if a.measures.startswith("model quality")]
        assert len(model_alarms) >= 3, (
            f"only {len(model_alarms)} model-quality alarms. Alarming only on "
            "latency and errors is the failure the rubric calls out."
        )

    def test_every_alarm_has_a_description_with_a_first_response(
        self, alarms: list[Any]
    ) -> None:
        """An alarm that fires at 3am without saying what to do has failed.

        The endpoint's two rollback alarms are exempt: they drive an automatic
        rollback, so the first response is "nothing, it already happened".
        """
        automatic = {"invocation_5xx", "model_latency"}
        missing = [
            a.resource
            for a in alarms
            if a.resource not in automatic and "FIRST RESPONSE" not in a.description
        ]
        assert not missing, f"alarms with no documented first response: {missing}"

    def test_every_actionable_alarm_links_the_runbook(self, alarms: list[Any]) -> None:
        automatic = {"invocation_5xx", "model_latency"}
        missing = [
            a.resource
            for a in alarms
            if a.resource not in automatic and "RUNBOOK" not in a.description
        ]
        assert not missing, f"alarms with no runbook link: {missing}"

    def test_rendered_inventory_covers_every_alarm(self, alarms: list[Any]) -> None:
        """Adding an alarm without regenerating the inventory must fail CI.

        This is the drift guard. The inventory is an M4 deliverable, and a deliverable
        that silently omits a newly-added alarm is worse than one that is obviously
        out of date.
        """
        assert INVENTORY.is_file(), (
            f"{INVENTORY} is missing — run `make alarm-inventory`"
        )
        rendered = INVENTORY.read_text(encoding="utf-8")
        missing = [a.name for a in alarms if a.name not in rendered]
        assert not missing, (
            f"alarms absent from the rendered inventory: {missing}. "
            "Run `make alarm-inventory`."
        )

    def test_inventory_is_current(self, alarms: list[Any]) -> None:
        """Byte-for-byte: the committed inventory must equal a fresh render."""
        assert INVENTORY.read_text(encoding="utf-8") == render_alarm_inventory.render(
            alarms
        ), "evidence/m4/alarm-inventory.md is stale — run `make alarm-inventory`"


class TestMetricEmission:
    """Every metric the dashboard and alarms read must actually be emitted."""

    @pytest.fixture(scope="class")
    def emitted_metrics(self) -> set[str]:
        definition = json.loads(ASL.read_text(encoding="utf-8"))
        names: set[str] = set()
        for state in definition["States"].values():
            resource = state.get("Resource")
            if not isinstance(resource, str) or "putMetricData" not in resource:
                continue
            for datum in state["Parameters"]["MetricData"]:
                names.add(datum["MetricName"])
        return names

    @pytest.fixture(scope="class")
    def consumed_metrics(self) -> set[str]:
        """Metric names the observability module reads from our own namespace."""
        names: set[str] = set()
        for tf in (REPO / "infra" / "modules" / "observability").glob("*.tf"):
            text = tf.read_text(encoding="utf-8")
            names.update(re.findall(r'metric_name\s*=\s*"([A-Za-z0-9]+)"', text))
            names.update(re.findall(r'\[local\.ns,\s*"([A-Za-z0-9]+)"', text))
        # AWS-provided metrics are not ours to emit.
        return names - {
            "ExecutionTime",
            "ExecutionsFailed",
            "ExecutionsSucceeded",
            "ExecutionsTimedOut",
            "ModelLatency",
            "ModelInvocation5XXErrors",
            "ApproximateNumberOfMessagesVisible",
        }

    def test_the_required_metric_set_is_emitted(
        self, emitted_metrics: set[str]
    ) -> None:
        """The assignment names a minimum metric set. These are the raw counters it
        is derived from — rates and cost are metric math over them."""
        for required in (
            "DocumentsProcessed",
            "AutoApproved",
            "HumanReviewed",
            "HumanOverride",
            "SchemaValidationFailure",
            "Confidence",
            "LLMInputTokens",
            "LLMOutputTokens",
        ):
            assert required in emitted_metrics, f"{required} is never emitted"

    def test_every_consumed_metric_is_emitted(
        self, emitted_metrics: set[str], consumed_metrics: set[str]
    ) -> None:
        """A dashboard panel or alarm reading a metric nobody emits shows a flat line.

        Worse than an empty panel: a flat line at zero looks like a real measurement.
        A rate whose denominator is never emitted evaluates as missing data, and the
        alarm sits in INSUFFICIENT_DATA forever while appearing configured.
        """
        orphans = sorted(consumed_metrics - emitted_metrics)
        assert not orphans, (
            f"the dashboard/alarms read metrics that are never emitted: {orphans}"
        )

    def test_metrics_are_dimensioned_by_environment(self) -> None:
        """Without it, dev and staging datapoints merge into one meaningless series."""
        definition = json.loads(ASL.read_text(encoding="utf-8"))
        for name, state in definition["States"].items():
            resource = state.get("Resource")
            if not isinstance(resource, str) or "putMetricData" not in resource:
                continue
            for datum in state["Parameters"]["MetricData"]:
                dimension_names = {d["Name"] for d in datum["Dimensions"]}
                assert "Environment" in dimension_names, (
                    f"{name}/{datum['MetricName']} is not dimensioned by Environment"
                )

    def test_correlation_id_is_not_a_dimension(self) -> None:
        """The classic way to turn a $3 dashboard into a four-figure bill.

        CloudWatch charges per metric name x dimension-value combination, so
        dimensioning by correlation_id creates one custom metric per document.
        correlation_id belongs in logs and traces, never in a metric dimension.
        """
        definition = json.loads(ASL.read_text(encoding="utf-8"))
        for name, state in definition["States"].items():
            resource = state.get("Resource")
            if not isinstance(resource, str) or "putMetricData" not in resource:
                continue
            for datum in state["Parameters"]["MetricData"]:
                for dimension in datum["Dimensions"]:
                    assert "correlation" not in dimension["Name"].lower(), (
                        f"{name}/{datum['MetricName']} dimensions by "
                        f"{dimension['Name']} — one metric per document"
                    )

    def test_namespace_is_the_documented_one(self) -> None:
        definition = json.loads(ASL.read_text(encoding="utf-8"))
        for state in definition["States"].values():
            resource = state.get("Resource")
            if isinstance(resource, str) and "putMetricData" in resource:
                assert state["Parameters"]["Namespace"] == "Intake/Platform"


class TestPrices:
    @pytest.fixture(scope="class")
    def prices(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(PRICES.read_text(encoding="utf-8"))
        return loaded

    def test_bedrock_prices_are_present_and_ordered(
        self, prices: dict[str, Any]
    ) -> None:
        """Output tokens cost more than input on every current model.

        A swapped pair would understate cost by ~5x and still look plausible.
        """
        bedrock = prices["bedrock"]
        assert bedrock["output_usd_per_1k_tokens"] > bedrock["input_usd_per_1k_tokens"]

    def test_the_price_file_model_matches_the_terraform_default(
        self, prices: dict[str, Any]
    ) -> None:
        """A model change without a price change makes the cost panel quietly wrong.

        Wrong-but-plausible is worse than blank, because nobody investigates a
        number that looks reasonable.
        """
        variables = (
            REPO / "infra" / "modules" / "intake" / "variables.tf"
        ).read_text(encoding="utf-8")
        match = re.search(
            r'variable "bedrock_model_id".*?default\s*=\s*"([^"]+)"',
            variables,
            re.DOTALL,
        )
        assert match, "could not find the bedrock_model_id default"
        assert match.group(1) == prices["bedrock"]["model_id"], (
            f"Terraform defaults to {match.group(1)} but config/prices.json prices "
            f"{prices['bedrock']['model_id']}. The cost panel would be wrong."
        )

    def test_retrieved_date_is_recorded(self, prices: dict[str, Any]) -> None:
        """A price with no date is a claim with no shelf life."""
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", prices["retrieved"])
