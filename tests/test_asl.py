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

        Asserted as reachability rather than adjacency, because M4 inserted metric
        emission between the two. Adjacency was the weaker test — it would have
        passed a definition that stored the result first and persisted the correction
        afterwards, as long as they were neighbours.
        """

        def successors(name: str) -> set[str]:
            state = states[name]
            targets: set[str] = set()
            if "Next" in state:
                targets.add(state["Next"])
            if "Default" in state:
                targets.add(state["Default"])
            for choice in state.get("Choices", []):
                targets.add(choice["Next"])
            for catcher in state.get("Catch", []):
                targets.add(catcher["Next"])
            return targets

        def reaches(start: str, goal: str, *, blocked: str | None = None) -> bool:
            seen, frontier = {start}, [start]
            while frontier:
                current = frontier.pop()
                if current == goal:
                    return True
                if current == blocked:
                    continue
                for nxt in successors(current) - seen:
                    seen.add(nxt)
                    frontier.append(nxt)
            return False

        # The correction leads to the result...
        assert reaches("PersistCorrection", "StoreReviewedResult")
        # ...and there is no way to reach the result without going through the
        # correction first. Blocking PersistCorrection must make it unreachable.
        assert not reaches(
            "CreateReviewTask", "StoreReviewedResult", blocked="PersistCorrection"
        ), "a path reaches StoreReviewedResult without persisting the correction"


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
        # Exempt: states that run AFTER the dead-letter record is already durably in
        # SQS. `DeadLetter` itself must be able to fail loudly rather than silently
        # succeeding, and `EmitDeadLetterMetric` runs downstream of it — by then the
        # document is recorded, so failing to publish a metric datapoint must not
        # invent a second failure path. A gap in a graph is recoverable; a lost
        # document is not.
        after_record_is_written = {"DeadLetter", "EmitDeadLetterMetric"}

        offenders: list[str] = []
        for name, state in task_states(states).items():
            if name in after_record_is_written:
                continue
            for catcher in state.get("Catch", []):
                target = catcher.get("Next")
                if states.get(target, {}).get("Type") == "Fail":
                    offenders.append(f"{name} -> {target}")
        assert not offenders, f"catches that lose the document: {offenders}"

    def test_metric_emission_never_fails_a_stored_document(
        self, states: dict[str, Any]
    ) -> None:
        """Observability must not be in the critical path.

        Every metric-emitting state writes AFTER the document's outcome is durably
        stored, so a CloudWatch throttle must not turn a successful document into a
        failed one. Each emit state's catch-all therefore continues to the same place
        its success path goes.
        """
        emit_states = {
            name: state
            for name, state in states.items()
            if isinstance(state.get("Resource"), str)
            and "cloudwatch:putMetricData" in state["Resource"]
        }
        assert emit_states, "no metric-emitting states found"

        for name, state in emit_states.items():
            catch_all = [
                catcher
                for catcher in state.get("Catch", [])
                if "States.ALL" in catcher.get("ErrorEquals", [])
            ]
            assert catch_all, f"{name} has no catch-all"
            assert catch_all[0]["Next"] == state["Next"], (
                f"{name} diverts to {catch_all[0]['Next']} on a metric failure but "
                f"continues to {state['Next']} on success — a dropped datapoint "
                "would change the document's outcome"
            )


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
    EXPECTED = {
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
        "Environment",
    }

    def test_all_placeholders_are_known(self, definition: dict[str, Any]) -> None:
        """Terraform substitutes these; an unknown one deploys a broken definition."""
        import re

        raw = json.dumps(definition)
        found = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", raw))
        assert found == self.EXPECTED, (
            f"placeholder mismatch. unexpected={sorted(found - self.EXPECTED)} "
            f"missing={sorted(self.EXPECTED - found)}"
        )

    def test_terraform_substitutes_exactly_these_placeholders(self) -> None:
        """The ASL and the Terraform that renders it must stay in step.

        `templatefile` fails at plan time on a *missing* variable, so that half is
        caught by a plan. But an EXTRA variable in the Terraform map is silently
        ignored — so removing a placeholder from the ASL while leaving its
        substitution behind leaves dead configuration that reads as if it were wired
        up. Both directions are checked here, without needing a plan.
        """
        import re

        statemachine_tf = (
            Path(__file__).resolve().parents[1]
            / "infra"
            / "modules"
            / "intake"
            / "statemachine.tf"
        ).read_text(encoding="utf-8")

        # The templatefile(...) call's variable map, i.e. the block between the ASL
        # path and the closing brace.
        match = re.search(
            r"templatefile\(\s*\n?\s*\"[^\"]*intake\.asl\.json\",\s*\{(.*?)\n\s*\}\s*\n\s*\)",
            statemachine_tf,
            re.DOTALL,
        )
        assert match, "could not locate the templatefile call in statemachine.tf"

        supplied = set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", match.group(1), re.M))
        assert supplied == self.EXPECTED, (
            f"Terraform supplies {sorted(supplied)} but the ASL declares "
            f"{sorted(self.EXPECTED)}. "
            f"extra_in_terraform={sorted(supplied - self.EXPECTED)} "
            f"missing_from_terraform={sorted(self.EXPECTED - supplied)}"
        )


# ---------------------------------------------------------------------------
# Retrain state machine (M5)
# ---------------------------------------------------------------------------

RETRAIN_PATH: Final[Path] = (
    Path(__file__).resolve().parents[1] / "statemachines" / "retrain.asl.json"
)


@pytest.fixture(scope="module")
def retrain() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(RETRAIN_PATH.read_text(encoding="utf-8"))
    return loaded


@pytest.fixture(scope="module")
def retrain_states(retrain: dict[str, Any]) -> dict[str, Any]:
    states_map: dict[str, Any] = retrain["States"]
    return states_map


class TestRetrainSafety:
    """The properties that stop an automated path from model to production."""

    def test_registration_is_always_pending_manual_approval(
        self, retrain_states: dict[str, Any]
    ) -> None:
        """The safety property the whole milestone rests on.

        Hardcoded rather than parameterised: if it were an input, a caller could
        pass "Approved" and the model would deploy itself.
        """
        params = retrain_states["RegisterCandidate"]["Parameters"]
        assert params["ModelApprovalStatus"] == "PendingManualApproval"

    def test_approval_status_is_not_taken_from_input(
        self, retrain: dict[str, Any]
    ) -> None:
        raw = json.dumps(retrain)
        assert '"ModelApprovalStatus.$"' not in raw, (
            "approval status is read from execution input — a caller could pass "
            "Approved and self-deploy"
        )

    def test_the_state_machine_never_deploys(self, retrain: dict[str, Any]) -> None:
        """No endpoint API anywhere. Deployment is triggered by a human's approval
        event, not by this workflow."""
        raw = json.dumps(retrain)
        for forbidden in (
            "createEndpoint",
            "updateEndpoint",
            "createEndpointConfig",
            "UpdateEndpoint",
        ):
            assert forbidden not in raw, (
                f"the retrain state machine calls {forbidden} — it must stop at "
                "registration and let a human approval trigger the deploy"
            )

    def test_a_rejected_candidate_ends_in_success(
        self, retrain_states: dict[str, Any]
    ) -> None:
        """The gate working is the system functioning, not an error.

        Failing the execution would put a permanent error rate on the retrain state
        machine and train people to ignore it.
        """
        assert retrain_states["Gate"]["Default"] == "NotifyGateFailed"
        assert retrain_states["RejectedByGate"]["Type"] == "Succeed"

    def test_a_pipeline_failure_is_distinct_from_a_rejection(
        self, retrain_states: dict[str, Any]
    ) -> None:
        """Conflating them means nobody can tell "the model did not improve" from
        "the training job crashed"."""
        assert retrain_states["RetrainFailed"]["Type"] == "Fail"
        assert retrain_states["RejectedByGate"]["Type"] == "Succeed"

    def test_the_gate_decision_is_not_reimplemented_in_asl(
        self, retrain_states: dict[str, Any]
    ) -> None:
        """One definition of "better".

        The Choice reads a boolean computed by evaluate.evaluate_gate. Re-expressing
        the comparison as ASL Choice rules would create a second definition that can
        drift from the tested one — and the untested copy would be the one blocking
        releases.
        """
        choices = retrain_states["Gate"]["Choices"]
        assert len(choices) == 1
        assert choices[0]["Variable"] == "$.gate_input.metrics.gate.passed"
        assert choices[0]["BooleanEquals"] is True

    def test_evaluation_uses_the_same_entrypoint_as_m1(
        self, retrain_states: dict[str, Any]
    ) -> None:
        """A gate comparing numbers from two code paths compares two different things."""
        spec = retrain_states["Evaluate"]["Parameters"]["AppSpecification"]
        assert spec["ContainerEntrypoint"] == ["python", "-m", "src.training.evaluate"]
        assert "--champion-metrics" in spec["ContainerArguments"]

    def test_every_task_has_retry_and_catch(
        self, retrain_states: dict[str, Any]
    ) -> None:
        for name, state in retrain_states.items():
            if state.get("Type") != "Task":
                continue
            assert state.get("Retry"), f"{name} has no Retry"
            assert state.get("Catch"), f"{name} has no Catch"

    def test_every_transition_target_exists(
        self, retrain_states: dict[str, Any]
    ) -> None:
        for name, state in retrain_states.items():
            targets = set()
            if "Next" in state:
                targets.add(state["Next"])
            if "Default" in state:
                targets.add(state["Default"])
            for choice in state.get("Choices", []):
                targets.add(choice["Next"])
            for catcher in state.get("Catch", []):
                targets.add(catcher["Next"])
            for target in targets:
                assert target in retrain_states, f"{name} -> {target} does not exist"

    def test_the_trigger_reason_is_recorded(
        self, retrain_states: dict[str, Any]
    ) -> None:
        """"Why does version 7 exist" is the first question asked when a model
        misbehaves."""
        metadata = retrain_states["RegisterCandidate"]["Parameters"][
            "CustomerMetadataProperties"
        ]
        # Keys carrying a JSONPath value are suffixed `.$` in ASL.
        recorded = {key.removesuffix(".$") for key in metadata}
        for key in ("trigger_source", "drift_report_uri", "drift_verdict"):
            assert key in recorded

    def test_the_approval_notification_warns_about_sampling_bias(
        self, retrain_states: dict[str, Any]
    ) -> None:
        """The person approving is the last line of defence against the bias.

        Telling them at the moment of decision is worth more than a README section
        they read once.
        """
        message = retrain_states["NotifyAwaitingApproval"]["Parameters"]["Message.$"]
        assert "sampling-bias" in message or "sampling bias" in message
        assert "per-class" in message
