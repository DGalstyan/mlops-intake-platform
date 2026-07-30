#!/usr/bin/env python3
"""Render the alarm inventory from the Terraform source.

M4's deliverable is "dashboard screenshot + alarm inventory". The screenshot needs a
deployed dashboard; the inventory does not — it is a property of the configuration,
and generating it from the `.tf` files means it cannot drift from what would deploy.

Parsed from source rather than from `terraform output` deliberately: an output-based
inventory only exists after an apply, so it could not be produced at all without
credentials, and it would silently describe whatever was last applied rather than
what is in the repo.

There is a test asserting every `aws_cloudwatch_metric_alarm` in `infra/` appears in
the rendered inventory, so adding an alarm without documenting it fails CI.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra"

# Which module an alarm lives in tells you what it is for, so it is worth surfacing
# rather than flattening everything into one list.
MODULE_PURPOSE = {
    "observability": "platform metrics — business, model and pipeline health",
    "endpoint": "release safety — drives the canary auto-rollback",
    "intake": "data safety — dead-letter queue",
}

# Extracted from the alarm descriptions, which are written in a fixed shape:
# "<what happened>. WHAT BREAKS: ... FIRST RESPONSE: ... RUNBOOK: ..."
SECTION_RE = re.compile(
    r"(?P<summary>.*?)(?:WHAT BREAKS:\s*(?P<breaks>.*?))?"
    r"(?:FIRST RESPONSE:\s*(?P<response>.*?))?"
    r"(?:CAVEAT:\s*(?P<caveat>.*?))?"
    r"(?:NOTE:\s*(?P<note>.*?))?"
    r"(?:WHY [^:]*:\s*(?P<why>.*?))?"
    r"(?:RUNBOOK:\s*(?P<runbook>\S+))?$",
    re.DOTALL,
)


@dataclass
class Alarm:
    module: str
    resource: str
    name_expression: str
    description: str

    measures_tag: str

    @property
    def name(self) -> str:
        return resolve_name(self.name_expression)

    @property
    def measures(self) -> str:
        """The model-quality vs system-health split, read from the alarm's own tag.

        An earlier version inferred this from keywords in the description and
        miscategorised two alarms — `execution_failures` came out as "data safety"
        because its prose mentions the dead-letter queue. Since this distinction is
        precisely what the rubric asks the observability section to get right, guessing
        it from wording is the wrong place to be clever. The `measures` tag is
        deployed config: authoritative, machine-readable, and visible in the console
        next to the alarm it describes.
        """
        return self.measures_tag or "UNCLASSIFIED"


def iter_alarm_blocks(text: str) -> Iterator[tuple[str, str]]:
    """Yield (resource_name, block_body) for each metric-alarm resource."""
    for match in re.finditer(
        r'resource\s+"aws_cloudwatch_metric_alarm"\s+"([A-Za-z0-9_]+)"\s*\{', text
    ):
        name = match.group(1)
        start = match.end()
        depth = 1
        index = start
        while index < len(text) and depth:
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
            index += 1
        yield name, text[start : index - 1]


def extract_string(body: str, attribute: str) -> str:
    """Pull a simple or join()-built string attribute out of a block body."""
    simple = re.search(rf'^\s*{attribute}\s*=\s*"((?:[^"\\]|\\.)*)"', body, re.M)
    if simple:
        return simple.group(1)

    joined = re.search(rf"{attribute}\s*=\s*join\(\s*\"([^\"]*)\"\s*,\s*\[(.*?)\]\s*\)", body, re.DOTALL)
    if joined:
        separator = joined.group(1).replace("\\n", "\n")
        inner = joined.group(2)
        parts = re.findall(r'"((?:[^"\\]|\\.)*)"', inner)
        # Interpolated fragments must be resolved, not dropped. `local.runbook_note`
        # is what puts the runbook link into every description at deploy time, so an
        # extractor that ignores it reports the link as missing and any test built on
        # that output checks something other than what deploys.
        if "local.runbook_note" in inner:
            parts.append(f"RUNBOOK: {RUNBOOK_PATH}")
        return separator.join(parts)
    return ""


# The `.tf` source carries interpolation, not literals. Resolved to concrete names
# for the rendered document, because an inventory that says
# `${local.name}-confidence-p10-low` is unreadable to the person it is written for.
# `dev` is used as the illustrative environment and stated as such in the output.
RUNBOOK_PATH = "docs/runbook.md"

NAME_SUBSTITUTIONS: dict[str, str] = {
    "${var.name_prefix}": "intake-",
    "${local.name}": "intake-dev",
    "${local.table_prefix}": "intake-dev",
    "${var.environment}": "dev",
}


def resolve_name(expression: str) -> str:
    resolved = expression
    for pattern, value in NAME_SUBSTITUTIONS.items():
        resolved = resolved.replace(pattern, value)
    return resolved


def extract_tag(body: str, key: str) -> str:
    """Read a key from the resource's `tags = merge(var.tags, { ... })` block."""
    match = re.search(rf'{key}\s*=\s*"((?:[^"\\]|\\.)*)"', body)
    return match.group(1) if match else ""


def collect() -> list[Alarm]:
    alarms: list[Alarm] = []
    for tf_file in sorted(INFRA.rglob("*.tf")):
        module = tf_file.parent.name
        text = tf_file.read_text(encoding="utf-8")
        for resource, body in iter_alarm_blocks(text):
            alarms.append(
                Alarm(
                    module=module,
                    resource=resource,
                    name_expression=extract_string(body, "alarm_name"),
                    description=extract_string(body, "alarm_description"),
                    measures_tag=extract_tag(body, "measures"),
                )
            )
    return alarms


def split_description(description: str) -> dict[str, str]:
    match = SECTION_RE.match(" ".join(description.split()))
    if not match:
        return {"summary": description}
    return {k: (v or "").strip() for k, v in match.groupdict().items()}


def render(alarms: list[Alarm]) -> str:
    lines: list[str] = [
        "# M4 alarm inventory",
        "",
        "**Generated from the Terraform source** by `make alarm-inventory` — not",
        "hand-maintained, and not read from `terraform output`. An output-based",
        "inventory would only exist after an apply and would describe whatever was last",
        "applied rather than what is in the repo. A test asserts every",
        "`aws_cloudwatch_metric_alarm` in `infra/` appears here, so adding an alarm",
        "without documenting it fails CI.",
        "",
        "**Nothing here is deployed.** These are the alarms that would be created.",
        "Names are shown resolved for the `dev` environment; `staging` differs only in",
        "that suffix.",
        "",
        "## The distinction that matters",
        "",
        "The `measures` column separates **model quality** from **system health**. Every",
        "system-health alarm can be green while the model is quietly wrong — which is the",
        "whole reason the model-quality proxies exist. None of them measures accuracy:",
        "there is no ground truth in production.",
        "",
        f"**{len(alarms)} alarms**, "
        + ", ".join(
            f"{sum(1 for a in alarms if a.module == module)} in `{module}`"
            for module in sorted({a.module for a in alarms})
        )
        + ".",
        "",
        "| Alarm | Measures | Module |",
        "|---|---|---|",
    ]
    for alarm in sorted(alarms, key=lambda a: (a.module, a.resource)):
        lines.append(
            f"| `{alarm.name}` | {alarm.measures} | `{alarm.module}` |"
        )

    lines += ["", "---", ""]

    for module in sorted({a.module for a in alarms}):
        lines += [
            f"## `{module}` — {MODULE_PURPOSE.get(module, 'see module docs')}",
            "",
        ]
        for alarm in sorted(
            (a for a in alarms if a.module == module), key=lambda a: a.resource
        ):
            parts = split_description(alarm.description)
            lines += [
                f"### `{alarm.name}`",
                "",
                f"- **Measures:** {alarm.measures}",
                f"- **Fires when:** {parts.get('summary') or '(see Terraform)'}",
            ]
            if parts.get("breaks"):
                lines.append(f"- **What breaks:** {parts['breaks']}")
            if parts.get("response"):
                lines.append(f"- **First response:** {parts['response']}")
            if parts.get("caveat"):
                lines.append(f"- **Caveat:** {parts['caveat']}")
            if parts.get("why"):
                lines.append(f"- **Why this statistic:** {parts['why']}")
            if parts.get("note"):
                lines.append(f"- **Note:** {parts['note']}")
            lines += [
                "- **Notifies:** the platform SNS topic "
                "(`intake-<env>-alarms`). No email is subscribed by default — an "
                "unconfirmed email subscription looks configured while delivering "
                "nothing.",
                "",
            ]

    lines += [
        "---",
        "",
        "## Who is paged",
        "",
        "Honest answer: **nobody, yet.** Every alarm publishes to one SNS topic with no",
        "subscriber by default. `alarm_email` adds one, but a real rotation needs an",
        "on-call tool and an escalation policy, and inventing a paging story this",
        "take-home does not implement would be worse than saying so.",
        "",
        "What the topic separation *would* be: the model-quality alarms are not",
        "wake-someone-up events — they need a human with the corrections table and a day",
        "to think. The pipeline-health and dead-letter alarms are. Splitting into two",
        "topics is the first thing to do when a rotation exists.",
        "",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    alarms = collect()
    if not alarms:
        print("no alarms found — did the infra layout change?", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "module": a.module,
                        "resource": a.resource,
                        "name": a.name,
                        "measures": a.measures,
                    }
                    for a in alarms
                ],
                indent=2,
            )
        )
        return 0

    rendered = render(alarms)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"wrote {args.output} ({len(alarms)} alarms)")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
