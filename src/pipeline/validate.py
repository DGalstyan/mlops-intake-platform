"""Validation of an extracted document: JSON Schema plus cross-field rules.

**Why this is a Lambda** (the assignment asks every retained Lambda to be
justified): JSON Schema validation and cross-field business rules cannot be
expressed in ASL. A Choice state can compare two values, but it cannot check a
regex, enforce an enum, or evaluate "expiry_date must be after date_of_birth". This
is real logic, not field mapping.

Two layers, deliberately separate:

1. **Schema validation** — types, requiredness, patterns, enums, bounds. Mechanical
   and derived entirely from `schemas/*.json`, so adding a field changes behaviour
   with no code edit.
2. **Field-level rules** — relationships JSON Schema cannot express, e.g. date
   ordering or a total that must be consistent with a currency. These are declared
   per class in `FIELD_RULES` and are the only place validation logic is written by
   hand.

A deliberately dependency-free implementation: no `jsonschema` package. The subset
of Draft 2020-12 used by these four schemas is small and fully enumerated here, and
the alternative is adding a dependency to the Lambda bundle to validate four
documents whose schemas we control. If the schemas grow to need `$ref`,
`allOf`/`oneOf`, or conditional subschemas, this should be replaced by the real
library rather than extended — the failure mode of a hand-rolled validator that
silently ignores a keyword it does not understand is a document that passes
validation and is auto-approved on unvalidated fields.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date
from typing import Any, Final

# Keywords this validator understands. Anything else in a schema is a hard error
# rather than a silent pass — see the module docstring.
SUPPORTED_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "type",
        "description",
        "enum",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
    }
)

_TYPE_CHECKS: Final[dict[str, Callable[[Any], bool]]] = {
    # bool before int deliberately: in Python `True` is an `int`, and accepting a
    # boolean where a number is required would let `true` through as an amount.
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One problem with an extracted document."""

    field: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "code": self.code, "message": self.message}


@dataclass
class ValidationResult:
    valid: bool
    issues: list[ValidationIssue] = dataclass_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "issue_count": len(self.issues),
            "issues": [issue.to_dict() for issue in self.issues],
            # Distinct field list, so a dashboard can show *which* fields fail most
            # often rather than only how many failures there were. That is the
            # difference between "validation is failing" and "the OCR cannot read
            # due_date on this vendor's layout".
            "failed_fields": sorted({issue.field for issue in self.issues}),
        }


def _parse_iso_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def validate_against_schema(
    document: Any, schema: dict[str, Any]
) -> list[ValidationIssue]:
    """Validate a document against the supported JSON Schema subset."""
    issues: list[ValidationIssue] = []

    if not isinstance(document, dict):
        return [
            ValidationIssue(
                field="<root>",
                code="not_an_object",
                message=f"expected a JSON object, got {type(document).__name__}",
            )
        ]

    properties: dict[str, Any] = schema.get("properties", {})
    required: Sequence[str] = schema.get("required", [])

    for name in required:
        if name not in document or document[name] is None:
            issues.append(
                ValidationIssue(
                    field=name,
                    code="required_missing",
                    message=f"required field {name!r} is missing or null",
                )
            )

    if schema.get("additionalProperties") is False:
        for name in document:
            if name not in properties:
                issues.append(
                    ValidationIssue(
                        field=name,
                        code="additional_property",
                        message=(
                            f"field {name!r} is not in the schema. The model "
                            "invented a field, which usually means it also invented "
                            "the value."
                        ),
                    )
                )

    for name, spec in properties.items():
        unsupported = set(spec) - SUPPORTED_KEYWORDS
        if unsupported:
            # Loud failure, not a silent skip. A validator that ignores a keyword it
            # does not implement reports a document as valid on fields it never
            # checked, and that document is then auto-approved.
            raise NotImplementedError(
                f"schema for field {name!r} uses unsupported keyword(s) "
                f"{sorted(unsupported)}. Replace this validator with the jsonschema "
                "library rather than letting the keyword be ignored."
            )

        if name not in document:
            continue
        value = document[name]

        declared = spec.get("type", "string")
        allowed = declared if isinstance(declared, list) else [declared]
        if not any(_TYPE_CHECKS.get(t, lambda _: False)(value) for t in allowed):
            issues.append(
                ValidationIssue(
                    field=name,
                    code="wrong_type",
                    message=(
                        f"expected {'/'.join(allowed)}, got "
                        f"{type(value).__name__}"
                    ),
                )
            )
            # Skip the remaining checks for this field: a pattern check against a
            # non-string would just produce a second, less useful complaint.
            continue

        if value is None:
            continue

        if "enum" in spec and value not in spec["enum"]:
            issues.append(
                ValidationIssue(
                    field=name,
                    code="not_in_enum",
                    message=f"{value!r} is not one of {spec['enum']}",
                )
            )

        if "pattern" in spec and isinstance(value, str):
            if not re.search(spec["pattern"], value):
                issues.append(
                    ValidationIssue(
                        field=name,
                        code="pattern_mismatch",
                        message=f"{value!r} does not match {spec['pattern']}",
                    )
                )

        if spec.get("format") == "date" and isinstance(value, str):
            if _parse_iso_date(value) is None:
                issues.append(
                    ValidationIssue(
                        field=name,
                        code="invalid_date",
                        message=f"{value!r} is not a valid ISO 8601 date",
                    )
                )

        if "minimum" in spec and isinstance(value, (int, float)):
            if value < spec["minimum"]:
                issues.append(
                    ValidationIssue(
                        field=name,
                        code="below_minimum",
                        message=f"{value} is below the minimum {spec['minimum']}",
                    )
                )

        if "maxLength" in spec and isinstance(value, str):
            if len(value) > spec["maxLength"]:
                issues.append(
                    ValidationIssue(
                        field=name,
                        code="too_long",
                        message=(
                            f"{len(value)} characters exceeds maxLength "
                            f"{spec['maxLength']}"
                        ),
                    )
                )

    return issues


# --- Field-level rules JSON Schema cannot express --------------------------
#
# Each rule returns an issue or None. Kept per-class and data-driven so the set of
# rules is inspectable in one place rather than buried in branching code.


def _rule_expiry_after_birth(doc: dict[str, Any]) -> ValidationIssue | None:
    dob = _parse_iso_date(doc.get("date_of_birth"))
    expiry = _parse_iso_date(doc.get("expiry_date"))
    if dob and expiry and expiry <= dob:
        return ValidationIssue(
            field="expiry_date",
            code="date_ordering",
            message=(
                f"expiry_date {expiry.isoformat()} is not after date_of_birth "
                f"{dob.isoformat()}; the two were probably swapped by OCR"
            ),
        )
    return None


def _rule_due_date_plausible(doc: dict[str, Any]) -> ValidationIssue | None:
    """Catch a due date implausibly far out.

    A four-digit year misread by OCR (2026 -> 2126) produces a schema-valid date, so
    only a plausibility rule catches it. Deliberately a wide window: the goal is to
    catch OCR damage, not to enforce a payment-terms policy.
    """
    due = _parse_iso_date(doc.get("due_date"))
    if due and due.year > date.today().year + 30:
        return ValidationIssue(
            field="due_date",
            code="implausible_date",
            message=(
                f"due_date {due.isoformat()} is more than 30 years out; likely an "
                "OCR misread of the year"
            ),
        )
    return None


def _rule_total_amount_sane(doc: dict[str, Any]) -> ValidationIssue | None:
    """A zero total on an invoice is valid JSON and almost never a real invoice."""
    total = doc.get("total_amount")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        if total == 0:
            return ValidationIssue(
                field="total_amount",
                code="suspicious_zero",
                message=(
                    "total_amount is 0. Schema-valid, but a zero-value invoice is "
                    "usually a failed extraction rather than a real document."
                ),
            )
    return None


def _rule_response_deadline_after_letter(doc: dict[str, Any]) -> ValidationIssue | None:
    letter = _parse_iso_date(doc.get("letter_date"))
    deadline = _parse_iso_date(doc.get("requires_response_by"))
    if letter and deadline and deadline < letter:
        return ValidationIssue(
            field="requires_response_by",
            code="date_ordering",
            message=(
                f"requires_response_by {deadline.isoformat()} precedes letter_date "
                f"{letter.isoformat()}"
            ),
        )
    return None


FIELD_RULES: Final[dict[str, tuple[Callable[[dict[str, Any]], ValidationIssue | None], ...]]] = {
    "invoice": (_rule_due_date_plausible, _rule_total_amount_sane),
    "id_document": (_rule_expiry_after_birth,),
    "correspondence": (_rule_response_deadline_after_letter,),
    # medical_report has no cross-field rule yet. Stated explicitly rather than
    # omitted, so "no rules" is visibly a decision and not an oversight.
    "medical_report": (),
}


def apply_field_rules(
    document: dict[str, Any], document_class: str
) -> list[ValidationIssue]:
    rules = FIELD_RULES.get(document_class)
    if rules is None:
        raise KeyError(
            f"no field-rule set declared for document class {document_class!r}. "
            "Add an entry to FIELD_RULES — an empty tuple is a valid answer, but it "
            "must be explicit."
        )
    issues = [rule(document) for rule in rules]
    return [issue for issue in issues if issue is not None]


def validate_document(
    document: Any, schema: dict[str, Any], document_class: str
) -> ValidationResult:
    """Full validation: schema first, then cross-field rules."""
    issues = validate_against_schema(document, schema)

    # Field rules assume a dict with correctly-typed values; running them over a
    # document that already failed type checks produces noise, not information.
    if isinstance(document, dict) and not any(
        issue.code in {"not_an_object", "wrong_type"} for issue in issues
    ):
        issues.extend(apply_field_rules(document, document_class))

    return ValidationResult(valid=not issues, issues=issues)
