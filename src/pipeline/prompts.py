"""Extraction prompts, rendered from the JSON schemas rather than hand-written.

The requirement is that "the extraction prompt/schema is data, not code" — you
should be able to add a document class, or a field, without editing Python, ASL or
Terraform. That is satisfied by making `schemas/*.json` the single source of truth
and *deriving* the prompt from it:

    schemas/invoice.json  ──render──>  prompt text + response JSON schema
                          └─────────>  validation rules (src/pipeline/validate.py)

So a new field is one edit to one schema file, and the prompt, the model's expected
response shape, and the validator all follow automatically. Hand-written prompts
drift from the schema the moment someone adds a field to one and not the other, and
that drift shows up as a validation failure rate nobody can explain.

The rendered prompts are uploaded to DynamoDB at deploy time and read by the intake
state machine with a direct SDK integration, so no Lambda sits in the extract path
purely to build a string.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

SCHEMA_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "schemas"

# Bumped when the prompt *template* changes in a way that could alter model output
# for an unchanged schema. Stored alongside each rendered prompt so a change in
# extraction quality can be attributed to a prompt revision rather than blamed on
# the model or the data.
PROMPT_TEMPLATE_VERSION: Final[str] = "1.0.0"

_INSTRUCTIONS: Final[str] = """\
You are extracting structured fields from a single business document.

The document text below was produced by OCR and may contain errors, broken words,
and out-of-order fragments. Extract only what the text actually supports.

Rules:
- Return a single JSON object and nothing else. No prose, no markdown fences.
- Use exactly the field names given in the schema below.
- If a value is genuinely not present in the document, use null. Do not guess, and
  do not infer a value from your own knowledge of what such documents usually say.
- Dates must be ISO 8601 (YYYY-MM-DD). If a date is ambiguous between day-first and
  month-first ordering, use null rather than picking one.
- Numeric amounts must be plain numbers without currency symbols or thousands
  separators.
- Do not copy the field descriptions into the values.

A null for an unsupported field is a correct answer. A plausible invention is the
worst possible answer, because it will pass schema validation and be
auto-approved."""


@dataclass(frozen=True, slots=True)
class ExtractionPrompt:
    """A rendered prompt for one document class."""

    document_class: str
    prompt: str
    response_schema: dict[str, Any]
    required_fields: tuple[str, ...]
    template_version: str
    schema_title: str

    def to_item(self) -> dict[str, Any]:
        """Shape stored in DynamoDB and read by the state machine's Extract state."""
        return {
            "document_class": self.document_class,
            "prompt": self.prompt,
            "response_schema": json.dumps(self.response_schema, sort_keys=True),
            "required_fields": list(self.required_fields),
            "template_version": self.template_version,
        }


def load_schema(document_class: str, *, schema_dir: Path | None = None) -> dict[str, Any]:
    directory = schema_dir or SCHEMA_DIR
    path = directory / f"{document_class}.json"
    if not path.is_file():
        available = sorted(p.stem for p in directory.glob("*.json"))
        raise FileNotFoundError(
            f"no schema for document class {document_class!r} at {path}. "
            f"Available: {available}"
        )
    schema: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return schema


def _describe_field(name: str, spec: dict[str, Any], *, required: bool) -> str:
    """One line per field: name, type, requiredness, constraints, description.

    Constraints are included because they change what the model should return —
    telling it a currency is `^[A-Z]{3}$` is the difference between "USD" and
    "US Dollars", and the second fails validation and lands the document in a human
    review queue for no good reason.
    """
    raw_type = spec.get("type", "string")
    if isinstance(raw_type, list):
        # ["string", "null"] means genuinely optional; say so plainly rather than
        # emitting a union the model has to interpret.
        non_null = [t for t in raw_type if t != "null"]
        type_text = "/".join(non_null) if non_null else "string"
        nullable = "null" in raw_type
    else:
        type_text = str(raw_type)
        nullable = False

    parts = [f"- {name} ({type_text}"]
    parts.append(", required" if required else ", optional")
    if nullable and not required:
        parts.append(", may be null")
    parts.append(")")

    constraints: list[str] = []
    if "enum" in spec:
        constraints.append("one of: " + ", ".join(str(v) for v in spec["enum"]))
    if "pattern" in spec:
        constraints.append(f"must match {spec['pattern']}")
    if "minimum" in spec:
        constraints.append(f"minimum {spec['minimum']}")
    if "maxLength" in spec:
        constraints.append(f"at most {spec['maxLength']} characters")

    line = "".join(parts)
    if spec.get("description"):
        line += f": {spec['description']}"
    if constraints:
        line += f" [{'; '.join(constraints)}]"
    return line


def render_prompt(
    document_class: str, *, schema_dir: Path | None = None
) -> ExtractionPrompt:
    """Render the prompt and response schema for one document class."""
    schema = load_schema(document_class, schema_dir=schema_dir)
    properties: dict[str, Any] = schema.get("properties", {})
    if not properties:
        raise ValueError(f"schema for {document_class!r} declares no properties")
    required = tuple(schema.get("required", []))

    unknown_required = sorted(set(required) - set(properties))
    if unknown_required:
        raise ValueError(
            f"schema for {document_class!r} marks {unknown_required} required but "
            "does not define them"
        )

    field_lines = [
        _describe_field(name, spec, required=name in required)
        for name, spec in properties.items()
    ]

    prompt = "\n\n".join(
        [
            _INSTRUCTIONS,
            f"Document class: {document_class}",
            f"Description: {schema.get('description', '(none)')}",
            "Fields to extract:\n" + "\n".join(field_lines),
            "Return only the JSON object.",
        ]
    )

    return ExtractionPrompt(
        document_class=document_class,
        prompt=prompt,
        response_schema=schema,
        required_fields=required,
        template_version=PROMPT_TEMPLATE_VERSION,
        schema_title=str(schema.get("title", document_class)),
    )


def render_all(*, schema_dir: Path | None = None) -> dict[str, ExtractionPrompt]:
    """Render every class that has a schema file.

    Driven by what is on disk rather than by a hardcoded list, so dropping in a
    fifth schema is genuinely all that is needed — which is the point of the
    "40 document types added by a customer success team" question.
    """
    directory = schema_dir or SCHEMA_DIR
    classes = sorted(p.stem for p in directory.glob("*.json"))
    if not classes:
        raise FileNotFoundError(f"no schemas found in {directory}")
    return {name: render_prompt(name, schema_dir=directory) for name in classes}
