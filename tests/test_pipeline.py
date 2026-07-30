"""Pipeline tests: OCR normalisation, model-output parsing, validation, review API,
and the consistency of the local simulator with the deployed ASL.

`TestSimulatorMatchesAsl` is the important one. A simulation that routes differently
from the state machine that actually deploys proves nothing, and the traces in
`evidence/m3/` are produced by the simulator — so these tests are what make that
evidence worth anything.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import simulate_intake
from src.config import AUTO_APPROVE_CONFIDENCE_THRESHOLD, DOCUMENT_CLASSES
from src.pipeline import prompts as prompts_module
from src.pipeline.handlers import (
    ModelOutputError,
    assemble_reading_order,
    normalize_ocr_handler,
    parse_model_json,
    validate_handler,
)
from src.pipeline.review_api import (
    ReviewApiError,
    build_task_output,
    submit_correction,
    validate_correction,
)
from src.pipeline.validate import (
    FIELD_RULES,
    apply_field_rules,
    validate_against_schema,
    validate_document,
)

ASL_PATH = Path(__file__).resolve().parents[1] / "statemachines" / "intake.asl.json"


def line_block(text: str, top: float, left: float) -> dict[str, Any]:
    return {
        "BlockType": "LINE",
        "Text": text,
        "Geometry": {
            "BoundingBox": {"Top": top, "Left": left, "Width": 0.3, "Height": 0.02}
        },
    }


class TestReadingOrder:
    def test_orders_top_to_bottom(self) -> None:
        blocks = [line_block("second", 0.5, 0.1), line_block("first", 0.1, 0.1)]
        assert assemble_reading_order(blocks) == ["first", "second"]

    def test_orders_left_to_right_within_a_row(self) -> None:
        """Two-column layouts are the reason this function exists.

        Textract's returned order is not guaranteed to be reading order, and
        interleaving two columns produces text that lands verbatim in the extraction
        prompt and in what a human reviewer reads.
        """
        blocks = [
            line_block("right", 0.30, 0.60),
            line_block("left", 0.302, 0.10),
        ]
        assert assemble_reading_order(blocks) == ["left", "right"]

    def test_row_banding_tolerates_slight_vertical_offset(self) -> None:
        blocks = [
            line_block("b", 0.300, 0.50),
            line_block("a", 0.3005, 0.10),
        ]
        assert assemble_reading_order(blocks) == ["a", "b"]

    def test_distinct_rows_are_not_merged(self) -> None:
        blocks = [
            line_block("row2left", 0.40, 0.10),
            line_block("row1right", 0.10, 0.60),
        ]
        assert assemble_reading_order(blocks) == ["row1right", "row2left"]

    def test_ignores_non_line_blocks(self) -> None:
        blocks = [
            line_block("keep", 0.1, 0.1),
            {"BlockType": "WORD", "Text": "drop"},
            {"BlockType": "PAGE"},
        ]
        assert assemble_reading_order(blocks) == ["keep"]

    def test_empty_input_is_not_an_error(self) -> None:
        assert assemble_reading_order([]) == []


class TestNormalizeOcrHandler:
    def test_returns_the_downstream_contract(self) -> None:
        result = normalize_ocr_handler(
            {"correlation_id": "c", "blocks": [line_block("hello world", 0.1, 0.1)]}
        )
        assert set(result) == {"text", "content_sha256", "char_count", "line_count"}

    def test_content_hash_is_stable_and_prefixed(self) -> None:
        event = {"correlation_id": "c", "blocks": [line_block("same text", 0.1, 0.1)]}
        first = normalize_ocr_handler(event)
        second = normalize_ocr_handler(event)
        assert first["content_sha256"] == second["content_sha256"]
        assert first["content_sha256"].startswith("sha256:")

    def test_hash_is_over_text_not_block_order(self) -> None:
        """Two scans of the same page differ in bytes but not in text.

        The hash exists to detect content-level duplicates, so it must be invariant
        to the order Textract happens to return blocks in.
        """
        a = normalize_ocr_handler(
            {
                "correlation_id": "c",
                "blocks": [line_block("one", 0.1, 0.1), line_block("two", 0.5, 0.1)],
            }
        )
        b = normalize_ocr_handler(
            {
                "correlation_id": "c",
                "blocks": [line_block("two", 0.5, 0.1), line_block("one", 0.1, 0.1)],
            }
        )
        assert a["content_sha256"] == b["content_sha256"]

    def test_empty_document_reports_zero(self) -> None:
        result = normalize_ocr_handler({"correlation_id": "c", "blocks": []})
        assert result["char_count"] == 0


class TestParseModelJson:
    def test_plain_object(self) -> None:
        payload = {"content": [{"type": "text", "text": '{"a": 1}'}]}
        assert parse_model_json(payload) == {"a": 1}

    def test_markdown_fenced(self) -> None:
        payload = {"content": [{"type": "text", "text": '```json\n{"a": 1}\n```'}]}
        assert parse_model_json(payload) == {"a": 1}

    def test_fence_without_language_tag(self) -> None:
        payload = {"content": [{"type": "text", "text": '```\n{"a": 1}\n```'}]}
        assert parse_model_json(payload) == {"a": 1}

    def test_surrounding_prose_is_tolerated(self) -> None:
        """Tolerated deliberately.

        The alternative is sending an otherwise-good extraction to a human because
        the model added a sentence.
        """
        payload = {
            "content": [
                {"type": "text", "text": 'Here you go: {"a": 1} — hope that helps.'}
            ]
        }
        assert parse_model_json(payload) == {"a": 1}

    def test_prose_only_is_rejected(self) -> None:
        """A model that returns no object has failed.

        Pretending otherwise would auto-approve an empty field set.
        """
        payload = {"content": [{"type": "text", "text": "I could not find those."}]}
        with pytest.raises(ModelOutputError, match="no JSON object"):
            parse_model_json(payload)

    def test_json_array_is_rejected(self) -> None:
        payload = {"content": [{"type": "text", "text": "[1, 2, 3]"}]}
        with pytest.raises(ModelOutputError):
            parse_model_json(payload)

    def test_raw_json_string_is_accepted(self) -> None:
        assert parse_model_json('{"content": [{"type": "text", "text": "{\\"a\\": 2}"}]}') == {
            "a": 2
        }

    def test_non_object_response_is_rejected(self) -> None:
        with pytest.raises(ModelOutputError, match="expected a response object"):
            parse_model_json(42)


class TestSchemaValidation:
    @pytest.fixture()
    def invoice_schema(self) -> dict[str, Any]:
        return prompts_module.load_schema("invoice")

    def test_a_good_document_passes(self, invoice_schema: dict[str, Any]) -> None:
        document = {
            "invoice_number": "INV-1",
            "total_amount": 10.0,
            "currency": "USD",
            "due_date": "2026-01-01",
            "vendor_name": "Acme",
        }
        assert validate_document(document, invoice_schema, "invoice").valid

    def test_missing_required_field_is_caught(
        self, invoice_schema: dict[str, Any]
    ) -> None:
        result = validate_document({"invoice_number": "INV-1"}, invoice_schema, "invoice")
        assert not result.valid
        assert any(i.code == "required_missing" for i in result.issues)

    def test_null_required_field_counts_as_missing(
        self, invoice_schema: dict[str, Any]
    ) -> None:
        """A model returning null for a required field has not extracted it.

        Treating null as present would auto-approve a document with empty fields.
        """
        document = {
            "invoice_number": None,
            "total_amount": 10.0,
            "currency": "USD",
            "due_date": "2026-01-01",
            "vendor_name": "Acme",
        }
        result = validate_document(document, invoice_schema, "invoice")
        assert any(
            i.code == "required_missing" and i.field == "invoice_number"
            for i in result.issues
        )

    def test_pattern_mismatch_is_caught(self, invoice_schema: dict[str, Any]) -> None:
        document = {
            "invoice_number": "INV-1",
            "total_amount": 10.0,
            "currency": "US Dollars",
            "due_date": "2026-01-01",
            "vendor_name": "Acme",
        }
        result = validate_document(document, invoice_schema, "invoice")
        assert any(i.code == "pattern_mismatch" for i in result.issues)

    def test_invented_field_is_caught(self, invoice_schema: dict[str, Any]) -> None:
        """additionalProperties=false catches hallucinated fields.

        A model that invents a field name usually invented the value too.
        """
        document = {
            "invoice_number": "INV-1",
            "total_amount": 10.0,
            "currency": "USD",
            "due_date": "2026-01-01",
            "vendor_name": "Acme",
            "vat_registration": "GB123",
        }
        result = validate_document(document, invoice_schema, "invoice")
        assert any(i.code == "additional_property" for i in result.issues)

    def test_boolean_is_not_accepted_as_a_number(self) -> None:
        """In Python `True` is an `int`; an amount of `true` must not pass."""
        schema = {
            "properties": {"amount": {"type": "number"}},
            "required": ["amount"],
            "additionalProperties": False,
        }
        result = validate_against_schema({"amount": True}, schema)
        assert any(i.code == "wrong_type" for i in result)

    def test_enum_violation_is_caught(self) -> None:
        schema = {
            "properties": {"kind": {"type": "string", "enum": ["a", "b"]}},
            "additionalProperties": False,
        }
        assert any(
            i.code == "not_in_enum" for i in validate_against_schema({"kind": "z"}, schema)
        )

    def test_invalid_date_is_caught(self) -> None:
        schema = {
            "properties": {"d": {"type": "string", "format": "date"}},
            "additionalProperties": False,
        }
        assert any(
            i.code == "invalid_date"
            for i in validate_against_schema({"d": "2026-13-45"}, schema)
        )

    def test_unsupported_keyword_raises_rather_than_passing(self) -> None:
        """The most important test in this class.

        A validator that ignores a keyword it does not implement reports a document
        as valid on fields it never checked, and that document is auto-approved. It
        must fail loudly instead.
        """
        schema = {"properties": {"x": {"type": "object", "$ref": "#/defs/thing"}}}
        with pytest.raises(NotImplementedError, match="unsupported keyword"):
            validate_against_schema({"x": {}}, schema)

    def test_non_object_document_is_reported_once(self) -> None:
        result = validate_against_schema(["not", "an", "object"], {"properties": {}})
        assert len(result) == 1
        assert result[0].code == "not_an_object"


class TestFieldRules:
    def test_every_class_declares_a_rule_set(self) -> None:
        """An empty tuple is fine; a missing entry is not.

        Silence should be a decision, not an oversight.
        """
        assert set(FIELD_RULES) == set(DOCUMENT_CLASSES)

    def test_expiry_before_birth_is_caught(self) -> None:
        issues = apply_field_rules(
            {"date_of_birth": "1990-01-01", "expiry_date": "1985-01-01"}, "id_document"
        )
        assert any(i.code == "date_ordering" for i in issues)

    def test_valid_ordering_passes(self) -> None:
        assert not apply_field_rules(
            {"date_of_birth": "1990-01-01", "expiry_date": "2030-01-01"}, "id_document"
        )

    def test_implausible_year_is_caught(self) -> None:
        """A four-digit year misread by OCR is schema-valid.

        Only a plausibility rule catches 2026 -> 2126.
        """
        issues = apply_field_rules({"due_date": "2199-01-01"}, "invoice")
        assert any(i.code == "implausible_date" for i in issues)

    def test_zero_total_is_flagged(self) -> None:
        issues = apply_field_rules({"total_amount": 0}, "invoice")
        assert any(i.code == "suspicious_zero" for i in issues)

    def test_response_deadline_before_letter_is_caught(self) -> None:
        issues = apply_field_rules(
            {"letter_date": "2026-06-01", "requires_response_by": "2026-05-01"},
            "correspondence",
        )
        assert any(i.code == "date_ordering" for i in issues)

    def test_unknown_class_raises(self) -> None:
        with pytest.raises(KeyError, match="no field-rule set"):
            apply_field_rules({}, "not_a_class")

    def test_rules_are_skipped_when_types_are_wrong(self) -> None:
        """Running cross-field rules over type-invalid data produces noise.

        A date-ordering complaint about a field that is an integer is not useful
        information for the reviewer.
        """
        schema = prompts_module.load_schema("id_document")
        document = {
            "document_type": "passport",
            "document_number": "X",
            "full_name": "Y",
            "date_of_birth": 12345,
            "expiry_date": "2030-01-01",
            "issuing_country": "GBR",
        }
        result = validate_document(document, schema, "id_document")
        assert any(i.code == "wrong_type" for i in result.issues)
        assert not any(i.code == "date_ordering" for i in result.issues)


class TestValidateHandler:
    def test_unparseable_output_becomes_a_validation_failure(self) -> None:
        """Not a Lambda error.

        A malformed model response should send the document to review, where a human
        can type the fields, rather than dead-lettering it.
        """
        result = validate_handler(
            {
                "correlation_id": "c",
                "document_class": "invoice",
                "response_schema": json.dumps(prompts_module.load_schema("invoice")),
                "model_output": {"content": [{"type": "text", "text": "sorry"}]},
            }
        )
        assert result["fields"] == {}
        assert result["validation"]["valid"] is False
        assert result["validation"]["issues"][0]["code"] == "unparseable_model_output"

    def test_valid_extraction_passes_through(self) -> None:
        fields = {
            "invoice_number": "INV-2",
            "total_amount": 5.0,
            "currency": "GBP",
            "due_date": "2026-03-03",
            "vendor_name": "Acme",
        }
        result = validate_handler(
            {
                "correlation_id": "c",
                "document_class": "invoice",
                "response_schema": json.dumps(prompts_module.load_schema("invoice")),
                "model_output": {
                    "content": [{"type": "text", "text": json.dumps(fields)}]
                },
            }
        )
        assert result["validation"]["valid"] is True
        assert result["fields"]["invoice_number"] == "INV-2"


class TestPrompts:
    def test_every_class_renders(self) -> None:
        rendered = prompts_module.render_all()
        assert set(rendered) == set(DOCUMENT_CLASSES)

    def test_prompt_mentions_every_field(self) -> None:
        """The prompt is derived from the schema, so it cannot drift from it."""
        prompt = prompts_module.render_prompt("invoice")
        schema = prompts_module.load_schema("invoice")
        for field_name in schema["properties"]:
            assert field_name in prompt.prompt

    def test_prompt_carries_constraints(self) -> None:
        """Telling the model the pattern is the difference between USD and US Dollars."""
        prompt = prompts_module.render_prompt("invoice")
        assert "^[A-Z]{3}$" in prompt.prompt

    def test_enum_values_appear(self) -> None:
        prompt = prompts_module.render_prompt("id_document")
        assert "passport" in prompt.prompt

    def test_required_fields_are_marked(self) -> None:
        prompt = prompts_module.render_prompt("invoice")
        assert "required" in prompt.prompt
        assert set(prompt.required_fields) == set(
            prompts_module.load_schema("invoice")["required"]
        )

    def test_missing_class_names_available_ones(self) -> None:
        with pytest.raises(FileNotFoundError, match="Available"):
            prompts_module.render_prompt("not_a_class")

    def test_dynamo_item_shape(self) -> None:
        item = prompts_module.render_prompt("invoice").to_item()
        assert set(item) == {
            "document_class",
            "prompt",
            "response_schema",
            "required_fields",
            "template_version",
        }


class TestReviewApiValidation:
    def _payload(self, **overrides: Any) -> dict[str, Any]:
        base = {
            "correlation_id": "b#k#v",
            "reviewer_id": "rev-1",
            "corrected_class": "invoice",
            "corrected_fields": {},
        }
        base.update(overrides)
        return base

    def test_accepts_a_valid_correction(self) -> None:
        assert validate_correction(self._payload())["reviewer_id"] == "rev-1"

    def test_reviewer_id_is_required(self) -> None:
        """Not defaulted to anonymous.

        The corrections table is labelled training data; an unattributable label
        cannot be audited when the model trained on it misbehaves.
        """
        with pytest.raises(ReviewApiError, match="reviewer_id is required"):
            validate_correction(self._payload(reviewer_id=""))

    def test_corrected_class_must_be_known(self) -> None:
        """A free-text class would become a poisoned training label."""
        with pytest.raises(ReviewApiError, match="corrected_class must be one of"):
            validate_correction(self._payload(corrected_class="invioce"))

    def test_correlation_id_is_required(self) -> None:
        with pytest.raises(ReviewApiError, match="correlation_id is required"):
            validate_correction(self._payload(correlation_id="  "))

    def test_corrected_fields_must_be_an_object(self) -> None:
        with pytest.raises(ReviewApiError, match="must be an object"):
            validate_correction(self._payload(corrected_fields=[1, 2]))

    def test_non_object_body_is_rejected(self) -> None:
        with pytest.raises(ReviewApiError, match="must be a JSON object"):
            validate_correction("nope")

    def test_prediction_correctness_is_computed_not_submitted(self) -> None:
        """The override rate must be the system's observation, not self-reported.

        M5 uses it as a concept-drift proxy; letting the client assert it would make
        that signal meaningless.
        """
        output = build_task_output(
            self._payload(corrected_class="invoice"),
            {"predicted_class": "correspondence"},
        )
        assert output["prediction_was_correct"] is False

        output = build_task_output(
            self._payload(corrected_class="invoice"), {"predicted_class": "invoice"}
        )
        assert output["prediction_was_correct"] is True


class TestSubmitCorrection:
    class Store:
        def __init__(self, item: dict[str, Any] | None) -> None:
            self.item = item
            self.completed: list[tuple[str, str]] = []

        def get_pending(self, correlation_id: str) -> dict[str, Any] | None:
            return self.item

        def mark_completed(self, correlation_id: str, reviewer_id: str) -> None:
            self.completed.append((correlation_id, reviewer_id))

    class Sfn:
        def __init__(self, error: Exception | None = None) -> None:
            self.error = error
            self.calls: list[dict[str, Any]] = []

        def send_task_success(self, *, taskToken: str, output: str) -> dict[str, Any]:
            if self.error:
                raise self.error
            self.calls.append({"token": taskToken, "output": json.loads(output)})
            return {}

        def send_task_failure(
            self, *, taskToken: str, error: str, cause: str
        ) -> dict[str, Any]:
            return {}

    def _payload(self) -> dict[str, Any]:
        return {
            "correlation_id": "b#k#v",
            "reviewer_id": "rev-1",
            "corrected_class": "invoice",
            "corrected_fields": {"invoice_number": "X"},
        }

    def test_resumes_the_execution_with_the_stored_token(self) -> None:
        """The token comes from the store, never from the caller.

        A task token is a capability: accepting one from the request would let anyone
        who guessed a token inject a correction into any document.
        """
        store = self.Store(
            {"task_token": "tok-abc", "predicted_class": "correspondence"}
        )
        sfn = self.Sfn()
        result = submit_correction(self._payload(), store=store, stepfunctions=sfn)

        assert sfn.calls[0]["token"] == "tok-abc"
        assert result["status"] == "RESUMED"
        assert result["prediction_was_correct"] is False

    def test_caller_supplied_token_is_ignored(self) -> None:
        store = self.Store({"task_token": "real", "predicted_class": "invoice"})
        sfn = self.Sfn()
        payload = self._payload() | {"task_token": "attacker-supplied"}
        submit_correction(payload, store=store, stepfunctions=sfn)
        assert sfn.calls[0]["token"] == "real"

    def test_unknown_review_is_404(self) -> None:
        with pytest.raises(ReviewApiError) as error:
            submit_correction(
                self._payload(), store=self.Store(None), stepfunctions=self.Sfn()
            )
        assert error.value.status == 404

    def test_timed_out_task_reports_conflict_not_success(self) -> None:
        """Reporting success would tell a reviewer their work was applied when the
        document has already dead-lettered."""

        class TaskTimedOut(Exception):
            pass

        store = self.Store({"task_token": "t", "predicted_class": "invoice"})
        with pytest.raises(ReviewApiError) as error:
            submit_correction(
                self._payload(),
                store=store,
                stepfunctions=self.Sfn(TaskTimedOut("TaskTimedOut")),
            )
        assert error.value.status == 409
        assert "dead-letter" in error.value.message

    def test_review_is_marked_complete_only_after_resuming(self) -> None:
        """Order matters.

        Marking complete first would leave a review closed while the execution still
        waits — it would eventually time out and dead-letter a document a human had
        already fixed.
        """

        class Boom(Exception):
            pass

        store = self.Store({"task_token": "t", "predicted_class": "invoice"})
        with pytest.raises(Boom):
            submit_correction(
                self._payload(), store=store, stepfunctions=self.Sfn(Boom("nope"))
            )
        assert store.completed == []

    def test_missing_token_is_a_conflict(self) -> None:
        store = self.Store({"predicted_class": "invoice"})
        with pytest.raises(ReviewApiError) as error:
            submit_correction(self._payload(), store=store, stepfunctions=self.Sfn())
        assert error.value.status == 409


class TestSimulatorMatchesAsl:
    """The simulator must route exactly as the deployed state machine does.

    The traces in evidence/m3/ come from the simulator, so if its conditions diverge
    from the ASL those traces are evidence of something that does not exist.
    """

    @pytest.fixture(scope="class")
    def states(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(ASL_PATH.read_text(encoding="utf-8"))
        states_map: dict[str, Any] = loaded["States"]
        return states_map

    def test_always_review_classes_agree(self, states: dict[str, Any]) -> None:
        asl_classes = {
            choice["StringEquals"]
            for choice in states["DecideOutcome"]["Choices"]
            if choice.get("Variable") == "$.classification.predicted_class"
        }
        assert asl_classes == set(simulate_intake.ALWAYS_REVIEW_CLASSES)

    def test_route_state_uses_the_same_always_review_set(
        self, states: dict[str, Any]
    ) -> None:
        route_classes = {
            choice["StringEquals"]
            for choice in states["Route"]["Choices"]
            if choice.get("Variable") == "$.classification.predicted_class"
        }
        assert route_classes == set(simulate_intake.ALWAYS_REVIEW_CLASSES)

    def test_review_reasons_match_the_asl_markers(self, states: dict[str, Any]) -> None:
        asl_reasons = {
            states[name]["Result"]
            for name in ("MarkSchemaFailure", "MarkBusinessRule", "MarkLowConfidence")
        }
        assert asl_reasons == {
            "SCHEMA_VALIDATION_FAILED",
            "BUSINESS_RULE_ALWAYS_REVIEW",
            "LOW_CONFIDENCE",
        }

    def test_decide_outcome_checks_conditions_in_the_same_order(
        self, states: dict[str, Any]
    ) -> None:
        """Order is semantic: it decides which reason a document is attributed to.

        The simulator checks validity, then business rule, then confidence. A
        schema-failing medical_report must be reported as SCHEMA_VALIDATION_FAILED,
        not as a business-rule review, or the M4 breakdown of why documents go to
        review is wrong.
        """
        choices = states["DecideOutcome"]["Choices"]
        assert choices[0]["Variable"] == "$.validated.validation.valid"
        assert choices[1]["Variable"] == "$.classification.predicted_class"
        assert choices[2]["Variable"] == "$.classification.auto_approve_eligible"

    def test_every_simulated_state_exists_in_the_asl(
        self, states: dict[str, Any], tmp_path: Path
    ) -> None:
        """Catches simulator drift directly.

        If the simulator records a state the deployed definition does not have, the
        traces in evidence/m3/ describe a workflow that does not exist. This is the
        check that would have caught M4's metric-emission states being added to the
        ASL and not to the simulator, or vice versa.

        Two step names are deliberately outside the state machine and exempt: the
        reviewer's out-of-band HTTP call, and the resumption it causes.
        """
        simulate_intake.main(["--output-dir", str(tmp_path)])

        out_of_band = {"ReviewApi.submitCorrection", "CreateReviewTask.resumed"}
        recorded: set[str] = set()
        for path in tmp_path.glob("trace-*.json"):
            recorded.update(json.loads(path.read_text())["states_entered"])

        unknown = sorted(recorded - set(states) - out_of_band)
        assert not unknown, (
            f"the simulator records states the ASL does not define: {unknown}. The "
            "traces in evidence/m3/ would describe a workflow that does not exist."
        )

    def test_metric_emission_appears_in_both(
        self, states: dict[str, Any], tmp_path: Path
    ) -> None:
        """The M4 counters must actually be emitted on the paths the traces claim."""
        simulate_intake.main(["--output-dir", str(tmp_path)])

        asl_emit_states = {
            name
            for name, state in states.items()
            if isinstance(state.get("Resource"), str)
            and "cloudwatch:putMetricData" in state["Resource"]
        }
        auto = json.loads((tmp_path / "trace-auto-approved.json").read_text())
        review = json.loads((tmp_path / "trace-human-corrected.json").read_text())

        assert "EmitAutoApprovedMetrics" in auto["states_entered"]
        assert "EmitAutoApprovedMetrics" in asl_emit_states
        assert any(
            state in review["states_entered"]
            for state in ("EmitConfirmedMetrics", "EmitOverriddenMetrics")
        )

    def test_confidence_threshold_comes_from_config(self) -> None:
        """The ASL gates on the endpoint's own boolean, not a duplicated number.

        That is what keeps one threshold constant in the system.
        """
        assert pytest.approx(0.80) == AUTO_APPROVE_CONFIDENCE_THRESHOLD
        raw = ASL_PATH.read_text(encoding="utf-8")
        assert "auto_approve_eligible" in raw
        assert "0.8" not in raw, (
            "the ASL hardcodes a confidence threshold; it must read the endpoint's "
            "auto_approve_eligible flag instead"
        )


class TestSimulationOutputs:
    """Assert the simulator produces the outcomes the evidence claims."""

    @pytest.fixture(scope="class")
    def run(self, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
        out = tmp_path_factory.mktemp("sim")
        simulate_intake.main(["--output-dir", str(out)])
        return {
            path.stem: json.loads(path.read_text())
            for path in out.glob("*.json")
        }

    def test_auto_approved_trace_reaches_auto_approve(self, run: dict[str, Any]) -> None:
        states = run["trace-auto-approved"]["states_entered"]
        assert "AutoApprove" in states
        assert "CreateReviewTask" not in states

    def test_human_corrected_trace_goes_through_review(
        self, run: dict[str, Any]
    ) -> None:
        states = run["trace-human-corrected"]["states_entered"]
        assert "CreateReviewTask" in states
        assert "PersistCorrection" in states
        assert "StoreReviewedResult" in states
        assert "AutoApprove" not in states

    def test_duplicate_delivery_short_circuits_before_any_billable_call(
        self, run: dict[str, Any]
    ) -> None:
        """Idempotency, and the cost argument for claiming first.

        A duplicate must not reach Textract, the endpoint or Bedrock.
        """
        states = run["trace-duplicate-delivery"]["states_entered"]
        assert states == ["Prepare", "ClaimIdempotencyKey", "DuplicateDelivery"]

    def test_duplicate_created_no_second_result_or_review_task(
        self, run: dict[str, Any]
    ) -> None:
        summary = run["simulation-summary"]
        # Four documents run, one of them a redelivery: three results, not four.
        assert summary["results_written"] == 3
        assert summary["review_tasks_created"] == 2
        assert summary["corrections_recorded"] == 2

    def test_schema_failure_routes_to_review(self, run: dict[str, Any]) -> None:
        trace = run["trace-schema-failure"]
        decide = [s for s in trace["steps"] if s["state"] == "DecideOutcome"][0]
        assert decide["review_reason"] == "SCHEMA_VALIDATION_FAILED"

    def test_corrections_record_the_original_prediction(
        self, run: dict[str, Any]
    ) -> None:
        trace = run["trace-human-corrected"]
        persist = [s for s in trace["steps"] if s["state"] == "PersistCorrection"][0]
        assert "original_predicted_class" in persist
        assert "was_prediction_correct" in persist
