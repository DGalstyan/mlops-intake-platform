"""Structural validation of the intake state machine.

`aws stepfunctions validate-state-machine-definition` checks syntax. These tests
check the *invariants the design depends on*, which is the more useful thing and
runs with no AWS account:

- every fallible state has Retry with full jitter and a Catch,
- every transition target exists and no state is orphaned,
- the dead-letter path only reads fields that are guaranteed to exist,
- the human-review state has a timeout so an abandoned review cannot hang forever,
- the review-task and result writes are both conditional, so one document cannot
  produce two review tasks or two results.

A syntax check would pass a definition that silently drops documents on a throttle.
These would not — which is why this is the test M6 nominates alongside the
inference contract test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

ASL_PATH: Final[Path] = (
    Path(__file__).resolve().parents[1] / "statemachines" / "intake.asl.json"
)

# States that call a remote service and can therefore fail transiently. Every one
# must carry Retry and Catch. Pass/Choice/Succeed/Fail cannot fail transiently.
FALLIBLE_TYPES: Final[frozenset[str]] = frozenset({"Task"})

TERMINAL_TYPES: Final[frozenset[str]] = frozenset({"Succeed", "Fail"})


@pytest.fixture(scope="module")
def definition() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(ASL_PATH.read_text(encoding="utf-8"))
    return loaded


@pytest.fixture(scope="module")
def states(definition: dict[str, Any]) -> dict[str, Any]:
    states_map: dict[str, Any] = definition["States"]
    return states_map


def task_states(states: dict[str, Any]) -> dict[str, Any]:
    return {
        name: state
        for name, state in states.items()
        if state.get("Type") in FALLIBLE_TYPES
    }


class TestWellFormed:
    def test_is_valid_json(self, definition: dict[str, Any]) -> None:
        assert definition["States"]

    def test_declares_a_start_state_that_exists(
        self, definition: dict[str, Any], states: dict[str, Any]
    ) -> None:
        assert definition["StartAt"] in states

    def test_has_a_top_level_timeout(self, definition: dict[str, Any]) -> None:
        """Without one, a stuck execution runs for a year.

        Step Functions' default is effectively unbounded, and an execution parked in
        a review state that nobody completes would otherwise never be reclaimed.
        """
        assert definition.get("TimeoutSeconds")

    def test_every_state_has_a_comment(self, states: dict[str, Any]) -> None:
        """ASL is read during incidents by people who did not write it.

        Trivial marker states are exempt: a Pass whose whole body is one Result
        field is self-describing.
        """
        undocumented = [
            name
            for name, state in states.items()
            if "Comment" not in state
            and state.get("Type") not in TERMINAL_TYPES
            and not (state.get("Type") == "Pass" and "Result" in state)
        ]
        assert not undocumented, f"states without a Comment: {undocumented}"


class TestTransitions:
    def _referenced(self, state: dict[str, Any]) -> set[str]:
        targets: set[str] = set()
        if "Next" in state:
            targets.add(state["Next"])
        if "Default" in state:
            targets.add(state["Default"])
        for choice in state.get("Choices", []):
            if "Next" in choice:
                targets.add(choice["Next"])
        for catcher in state.get("Catch", []):
            if "Next" in catcher:
                targets.add(catcher["Next"])
        return targets

    def test_every_transition_target_exists(self, states: dict[str, Any]) -> None:
        """A typo'd Next is a runtime failure on a real document, not a deploy error."""
        broken: list[str] = []
        for name, state in states.items():
            for target in self._referenced(state):
                if target not in states:
                    broken.append(f"{name} -> {target}")
        assert not broken, f"transitions to non-existent states: {broken}"

    def test_no_orphan_states(
        self, definition: dict[str, Any], states: dict[str, Any]
    ) -> None:
        """Every state must be reachable from StartAt.

        An unreachable state is usually a half-finished refactor: the logic someone
        believes is running is not.
        """
        reachable = {definition["StartAt"]}
        frontier = [definition["StartAt"]]
        while frontier:
            current = frontier.pop()
            for target in self._referenced(states[current]):
                if target not in reachable:
                    reachable.add(target)
                    frontier.append(target)
        orphans = sorted(set(states) - reachable)
        assert not orphans, f"unreachable states: {orphans}"

    def test_every_non_terminal_state_continues_somewhere(
        self, states: dict[str, Any]
    ) -> None:
        dead_ends = [
            name
            for name, state in states.items()
            if state.get("Type") not in TERMINAL_TYPES
            and not state.get("End")
            and "Next" not in state
            and "Default" not in state
            and not state.get("Choices")
        ]
        assert not dead_ends, f"states with no continuation: {dead_ends}"


class TestRetryPolicies:
    """The assignment's requirement: Retry with jitter on every fallible state."""

    def test_every_task_state_has_retry(self, states: dict[str, Any]) -> None:
        missing = [
            name for name, state in task_states(states).items() if not state.get("Retry")
        ]
        assert not missing, f"Task states with no Retry: {missing}"

    def test_every_retrier_uses_full_jitter(self, states: dict[str, Any]) -> None:
        """Without jitter, concurrent executions retry in lockstep.

        A batch S3 upload starts many executions at once; synchronised retries
        re-throttle each other and can fail to drain at all. This is the specific
        failure the assignment means by "throttling must not lose a document".
        """
        offenders: list[str] = []
        for name, state in task_states(states).items():
            for index, retrier in enumerate(state.get("Retry", [])):
                if retrier.get("JitterStrategy") != "FULL":
                    offenders.append(f"{name}[Retry {index}]")
        assert not offenders, f"retriers without FULL jitter: {offenders}"

    def test_every_retrier_backs_off_at_least_twofold(
        self, states: dict[str, Any]
    ) -> None:
        offenders: list[str] = []
        for name, state in task_states(states).items():
            for index, retrier in enumerate(state.get("Retry", [])):
                if float(retrier.get("BackoffRate", 1)) < 2:
                    offenders.append(f"{name}[Retry {index}]")
        assert not offenders, f"retriers with BackoffRate < 2: {offenders}"

    def test_throttling_is_retried_on_every_throttleable_service(
        self, states: dict[str, Any]
    ) -> None:
        """Textract, Bedrock and SageMaker Runtime all throttle. All must be retried."""
        expectations = {
            "ExtractText": "Textract.ThrottlingException",
            "Extract": "Bedrock.ThrottlingException",
            "Classify": "SageMakerRuntime.ThrottlingException",
        }
        for state_name, expected_error in expectations.items():
            retried = {
                error
                for retrier in states[state_name].get("Retry", [])
                for error in retrier.get("ErrorEquals", [])
            }
            assert expected_error in retried, (
                f"{state_name} does not retry {expected_error}; a throttle would "
                "lose the document"
            )

    def test_bedrock_and_textract_cap_their_backoff(
        self, states: dict[str, Any]
    ) -> None:
        """Unbounded exponential backoff on 6 attempts reaches absurd delays.

        Without MaxDelaySeconds, attempt 6 at BackoffRate 2 from a 3s base waits
        ~96s, and the total execution can exceed the review timeout.
        """
        for state_name in ("ExtractText", "Extract"):
            throttle_retriers = [
                retrier
                for retrier in states[state_name]["Retry"]
                if any("Throttl" in error for error in retrier.get("ErrorEquals", []))
            ]
            assert throttle_retriers, f"{state_name} has no throttle retrier"
            for retrier in throttle_retriers:
                assert retrier.get("MaxDelaySeconds"), (
                    f"{state_name} throttle retrier has no MaxDelaySeconds"
                )


class TestCatchPolicies:
    def test_every_task_state_has_a_catch(self, states: dict[str, Any]) -> None:
        missing = [
            name for name, state in task_states(states).items() if not state.get("Catch")
        ]
        assert not missing, f"Task states with no Catch: {missing}"

    def test_every_task_has_a_catch_all(self, states: dict[str, Any]) -> None:
        """A specific catch list leaves unlisted errors uncaught.

        Every Task must end with a States.ALL catcher, or a novel error code from a
        service update drops the document.
        """
        missing: list[str] = []
        for name, state in task_states(states).items():
            caught = {
                error
                for catcher in state.get("Catch", [])
                for error in catcher.get("ErrorEquals", [])
            }
            if "States.ALL" not in caught:
                missing.append(name)
        assert not missing, f"Task states without a States.ALL catcher: {missing}"

    def test_catch_all_is_last(self, states: dict[str, Any]) -> None:
        """Catchers are evaluated in order; States.ALL first would shadow the rest."""
        offenders: list[str] = []
        for name, state in task_states(states).items():
            catchers = state.get("Catch", [])
            for index, catcher in enumerate(catchers[:-1]):
                if "States.ALL" in catcher.get("ErrorEquals", []):
                    offenders.append(f"{name}[Catch {index}]")
        assert not offenders, f"States.ALL catcher is not last in: {offenders}"

    def test_every_catch_preserves_the_error(self, states: dict[str, Any]) -> None:
        """Without ResultPath the error replaces the state input.

        The dead-letter record would then contain the error and nothing about the
        document — no correlation id, no source key, nothing to debug with.
        """
        offenders: list[str] = []
        for name, state in task_states(states).items():
            for index, catcher in enumerate(state.get("Catch", [])):
                if "ResultPath" not in catcher:
                    offenders.append(f"{name}[Catch {index}]")
        assert not offenders, f"catchers that discard the document context: {offenders}"


class TestIdempotency:
    def test_the_ledger_claim_is_conditional(self, states: dict[str, Any]) -> None:
        claim = states["ClaimIdempotencyKey"]
        assert "attribute_not_exists" in claim["Parameters"]["ConditionExpression"]

    def test_the_claim_happens_before_any_paid_call(
        self, definition: dict[str, Any], states: dict[str, Any]
    ) -> None:
        """A duplicate must cost one DynamoDB write, not an OCR plus a Bedrock call.

        Walks the happy path from StartAt and asserts the ledger claim is reached
        before Textract, SageMaker or Bedrock.
        """
        paid = {"ExtractText", "Classify", "Extract"}
        current = definition["StartAt"]
        seen: list[str] = []
        for _ in range(len(states)):
            seen.append(current)
            if current == "ClaimIdempotencyKey":
                break
            nxt = states[current].get("Next")
            if not nxt:
                break
            current = nxt
        assert "ClaimIdempotencyKey" in seen, "ledger claim is not on the entry path"
        assert not (set(seen) & paid), (
            f"a billable call happens before the idempotency claim: {set(seen) & paid}"
        )

    def test_a_duplicate_succeeds_rather_than_fails(
        self, states: dict[str, Any]
    ) -> None:
        """Duplicate deliveries are routine, not errors.

        Failing them would put a permanent error rate on the state machine's metrics
        and make a real failure impossible to see.
        """
        claim = states["ClaimIdempotencyKey"]
        conditional = [
            catcher
            for catcher in claim["Catch"]
            if "DynamoDb.ConditionalCheckFailedException" in catcher["ErrorEquals"]
        ]
        assert conditional, "conditional-check failure is not caught"
        assert conditional[0]["Next"] == "DuplicateDelivery"
        assert states["DuplicateDelivery"].get("End") is True
        assert states["DuplicateDelivery"]["Type"] == "Pass"

    def test_review_task_creation_is_conditional(
        self, states: dict[str, Any]
    ) -> None:
        """One document must never produce two review tasks.

        The assignment calls this out explicitly, and it is the half of idempotency
        that is easy to forget — a second review task wastes a human's time and
        produces two conflicting corrections.
        """
        review = states["CreateReviewTask"]
        assert "attribute_not_exists" in review["Parameters"]["ConditionExpression"]

    def test_result_write_is_conditional(self, states: dict[str, Any]) -> None:
        assert (
            "attribute_not_exists"
            in states["AutoApprove"]["Parameters"]["ConditionExpression"]
        )


class TestHumanReview:
    def test_uses_wait_for_task_token(self, states: dict[str, Any]) -> None:
        assert states["CreateReviewTask"]["Resource"].endswith(".waitForTaskToken")

    def test_passes_the_task_token_into_the_queue(
        self, states: dict[str, Any]
    ) -> None:
        """The reviewer's API needs the token to resume the execution."""
        item = states["CreateReviewTask"]["Parameters"]["Item"]
        assert item["task_token"]["S.$"] == "$$.Task.Token"

    def test_has_a_timeout(self, states: dict[str, Any]) -> None:
        """An abandoned review must dead-letter, not hang forever.

        Without a timeout the execution waits until the state machine's own limit,
        holding a task token that nobody will ever use, and the document is
        invisibly stuck rather than visibly failed.
        """
        assert states["CreateReviewTask"].get("TimeoutSeconds")

    def test_timeout_dead_letters(self, states: dict[str, Any]) -> None:
        timeout_catchers = [
            catcher
            for catcher in states["CreateReviewTask"]["Catch"]
            if "States.Timeout" in catcher["ErrorEquals"]
        ]
        assert timeout_catchers, "review timeout is not caught"
        assert timeout_catchers[0]["Next"] == "DeadLetter"

    def test_corrections_capture_full_provenance(
        self, states: dict[str, Any]
    ) -> None:
        """Labelled training data needs more than the corrected label.

        Without the original prediction and its confidence, the retraining loop
        cannot tell a confirmation from a correction — and the override rate is the
        signal M5's concept-drift proxy is built on.
        """
        item = states["PersistCorrection"]["Parameters"]["Item"]
        for required in (
            "reviewer_id",
            "reviewed_at",
            "original_predicted_class",
            "original_confidence",
            "corrected_class",
            "was_prediction_correct",
            "document_text",
            "source_key",
        ):
            assert required in item, f"corrections record is missing {required}"

    def test_correction_is_persisted_before_the_result(
        self, states: dict[str, Any]
    ) -> None:
        """Order matters: the reviewer's labour is the harder thing to recreate.

        A document can be re-delivered; a human's correction cannot be recovered if
        it is dropped.
        """
        assert states["PersistCorrection"]["Next"] == "StoreReviewedResult"


class TestDeadLetterPath:
    def test_dead_letter_only_reads_guaranteed_fields(
        self, definition: dict[str, Any], states: dict[str, Any]
    ) -> None:
        """The regression this test exists for.

        The dead-letter state reads $.ocr and $.classification to record how far the
        document got. JSONPath references to absent fields fail the state — so if
        those are not seeded up front, a failure during OCR fails the dead-letter
        write too, and the document is lost exactly when the dead-letter path is the
        only thing that could save it. An earlier revision had this bug.
        """
        seeded = states[definition["StartAt"]]["Parameters"]
        body = states["DeadLetter"]["Parameters"]["MessageBody"]

        referenced_roots = set()
        for value in body.values():
            if not isinstance(value, str):
                continue
            # `$$.` is the context object (Execution, State, StateMachine, Task).
            # Always populated by the service, so it is not an input dependency —
            # blank it out before looking for `$.` state-input references.
            cleaned = value.replace("$$.", "<<CONTEXT>>")
            for token in cleaned.split("$.")[1:]:
                root = token.split(")")[0].split(",")[0].split(".")[0].strip()
                root = root.strip("'\" []")
                if root:
                    referenced_roots.add(root)

        seeded_roots = {key.removesuffix(".$") for key in seeded}
        # `error` is written by the Catch that routes here, so it is always present.
        allowed = seeded_roots | {"error", "dlq_error"}
        missing = sorted(referenced_roots - allowed)
        assert not missing, (
            f"DeadLetter reads fields not guaranteed to exist: {missing}. "
            f"Seed them in the {definition['StartAt']} state."
        )

    def test_dead_letter_carries_enough_to_debug(
        self, states: dict[str, Any]
    ) -> None:
        body = states["DeadLetter"]["Parameters"]["MessageBody"]
        for required in (
            "correlation_id",
            "execution_arn",
            "error",
            "source_bucket",
            "source_key",
        ):
            assert any(key.startswith(required) for key in body), (
                f"dead-letter message has no {required}"
            )

    def test_every_task_failure_reaches_the_dead_letter_or_review(
        self, states: dict[str, Any]
    ) -> None:
        """No Task may catch straight to a terminal Fail.

        A document that fails must leave a record. Going directly to Fail loses it.
        """
        offenders: list[str] = []
        for name, state in task_states(states).items():
            if name == "DeadLetter":
                continue
            for catcher in state.get("Catch", []):
                target = catcher.get("Next")
                if states.get(target, {}).get("Type") == "Fail":
                    offenders.append(f"{name} -> {target}")
        assert not offenders, f"catches that lose the document: {offenders}"


class TestDirectSdkIntegrations:
    def test_only_the_two_justified_lambdas_exist(
        self, states: dict[str, Any]
    ) -> None:
        """Glue-only Lambdas lose points; this pins the count.

        Adding a third Lambda should require deleting this assertion and justifying
        it in the ASL comment and the decision log — which is the point.
        """
        lambda_states = sorted(
            name
            for name, state in states.items()
            if isinstance(state.get("Resource"), str)
            and "lambda:invoke" in state["Resource"]
        )
        assert lambda_states == ["NormalizeOcr", "ValidateExtraction"], (
            f"unexpected Lambda states: {lambda_states}. Every Lambda must be "
            "justified in one line in the ASL comment."
        )

    def test_aws_services_use_direct_sdk_integrations(
        self, states: dict[str, Any]
    ) -> None:
        expected = {
            "ExtractText": "textract",
            "Classify": "sagemakerruntime",
            "FetchExtractionPrompt": "dynamodb",
            "Extract": "bedrock",
            "AutoApprove": "dynamodb",
            "PersistCorrection": "dynamodb",
            "StoreReviewedResult": "dynamodb",
            "DeadLetter": "sqs",
        }
        for state_name, service in expected.items():
            resource = states[state_name]["Resource"]
            assert service in resource, (
                f"{state_name} should use a direct {service} integration, got "
                f"{resource}"
            )

    def test_routing_uses_choice_states_not_a_lambda(
        self, states: dict[str, Any]
    ) -> None:
        """Confidence and business-rule routing is expressible in ASL."""
        assert states["Route"]["Type"] == "Choice"
        assert states["DecideOutcome"]["Type"] == "Choice"


class TestCorrelationId:
    def test_is_threaded_into_bedrock(self, states: dict[str, Any]) -> None:
        """Without it, LLM cost and latency cannot be attributed per document."""
        extract = json.dumps(states["Extract"])
        assert "correlation_id" in extract or "$.correlation_id" in extract

    def test_is_passed_to_the_endpoint(self, states: dict[str, Any]) -> None:
        params = states["Classify"]["Parameters"]
        assert params.get("CustomAttributes.$") == "$.correlation_id"

    def test_is_recorded_on_every_persisted_record(
        self, states: dict[str, Any]
    ) -> None:
        for state_name in (
            "AutoApprove",
            "CreateReviewTask",
            "PersistCorrection",
            "StoreReviewedResult",
        ):
            item = states[state_name]["Parameters"]["Item"]
            assert "correlation_id" in item, f"{state_name} does not record it"


class TestPlaceholders:
    def test_all_placeholders_are_known(self, definition: dict[str, Any]) -> None:
        """Terraform substitutes these; an unknown one deploys a broken definition."""
        import re

        raw = json.dumps(definition)
        found = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", raw))
        expected = {
            "LedgerTable",
            "ResultsTable",
            "ReviewQueueTable",
            "CorrectionsTable",
            "PromptsTable",
            "EndpointName",
            "BedrockModelId",
            "DeadLetterQueueUrl",
            "NormalizeOcrFunctionArn",
            "ValidateFunctionArn",
        }
        assert found == expected, (
            f"placeholder mismatch. unexpected={sorted(found - expected)} "
            f"missing={sorted(expected - found)}"
        )
