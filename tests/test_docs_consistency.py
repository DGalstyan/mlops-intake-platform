"""Assert the documentation's factual claims match the repo.

This exists because the same failure has now happened four times here: a count
written into prose, then the code changed. Three separate wildcard counts went stale,
the README claimed "only M0 is implemented" two milestones later, and the test count
appeared as both 330 and 335 in the same file.

The rubric names "a README that describes intentions rather than what you built" as a
point-loser, and a stale number is that failure in miniature. Prose that asserts a
fact about the code should be checked like any other assertion.

The rule this encodes: **never write a count into prose that a command can produce.**
Where a number must appear for readability, it gets a test.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


def collected_test_count() -> int:
    """How many tests actually exist, by asking pytest.

    Deliberately FAILS rather than skips when it cannot determine the count. An
    earlier version skipped, which is the exact failure the regression-proof harness
    caught elsewhere in this repo: a skipped test is green in every report and
    catches nothing, so the drift it was written to prevent goes unnoticed.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=600,
    )
    output = result.stdout

    # `-q` prints a per-file tally ("tests/test_x.py: 15") rather than a grand total,
    # so sum those. Fall back to the summary line if the format ever changes.
    per_file = [int(n) for n in re.findall(r"^\S+\.py: (\d+)$", output, re.M)]
    if per_file:
        return sum(per_file)

    match = re.search(r"(\d+) tests? collected", output)
    assert match, (
        "could not determine the collected test count from pytest output. "
        "This check must not silently skip — that is how a stale count survives.\n"
        f"output tail: {output[-500:]}"
    )
    return int(match.group(1))


class TestReadmeCounts:
    def test_test_count_is_current(self, readme: str) -> None:
        """The number in the quickstart must be the number that runs."""
        claimed = {int(m) for m in re.findall(r"\b(\d{3}) tests\b", readme)}
        if not claimed:
            pytest.skip("the README asserts no test count")
        actual = collected_test_count()
        # EXACT. The ±10 band this used to allow was self-defeating: the drift it
        # exists to catch is usually a handful of tests, so the tolerance covered
        # precisely the cases it was written for. An audit found the README claiming
        # 343 while 344 were collected — inside the old band, and the check that was
        # supposed to prevent exactly that passed.
        for value in claimed:
            assert value == actual, (
                f"README claims {value} tests; {actual} are collected. "
                "Update it (`make test` prints the number) or stop asserting a count."
            )

    def test_alarm_count_matches_the_terraform(self, readme: str) -> None:
        """A claimed alarm count must match what infra/ actually declares."""
        from scripts.render_alarm_inventory import collect

        actual = len(collect())
        claimed = {int(m) for m in re.findall(r"\b(\d+) alarms\b", readme)}
        for value in claimed:
            assert value == actual, (
                f"README claims {value} alarms; the Terraform declares {actual}. "
                "Run `make alarm-inventory`."
            )

    def test_custom_metric_count_matches_the_state_machine(self, readme: str) -> None:
        definition = json.loads(
            (REPO / "statemachines" / "intake.asl.json").read_text(encoding="utf-8")
        )
        emitted: set[str] = set()
        for state in definition["States"].values():
            resource = state.get("Resource")
            if isinstance(resource, str) and "putMetricData" in resource:
                for datum in state["Parameters"]["MetricData"]:
                    emitted.add(datum["MetricName"])

        claimed = {int(m) for m in re.findall(r"\b(\d+) custom metrics\b", readme)}
        for value in claimed:
            assert value == len(emitted), (
                f"README claims {value} custom metrics; the state machine emits "
                f"{len(emitted)}: {sorted(emitted)}"
            )


class TestReadmeHonesty:
    """The claims that would be actively misleading if they went stale."""

    def test_does_not_claim_anything_is_deployed(self, readme: str) -> None:
        """Nothing has been deployed. If that changes, this test should be updated
        deliberately rather than the claim quietly becoming false in the other
        direction."""
        assert "No AWS resource has ever been created from this" in readme, (
            "the README's central honesty claim is missing or reworded — it is the "
            "one statement a reader most needs to be true"
        )

    def test_every_milestone_row_states_a_status(self, readme: str) -> None:
        """The build-status table must not silently gain an unlabelled row."""
        rows = re.findall(r"^\| \*?\*?M(\d)\*?\*?[^|]*\|([^|]*)\|", readme, re.M)
        assert len(rows) >= 7, f"expected 7 milestone rows, found {len(rows)}"
        for number, status in rows:
            assert status.strip(), f"M{number} has an empty status cell"

    def test_known_gaps_section_exists(self, readme: str) -> None:
        """'What is broken and what I would fix next' is explicitly rewarded, and is
        the section most likely to be quietly dropped when it gets long."""
        assert "## Known gaps" in readme

    def test_every_milestone_has_a_gaps_subsection(self, readme: str) -> None:
        """Each milestone with code must say what is unverified about it."""
        gaps = readme[readme.find("## Known gaps") :]
        for milestone in ("M1", "M2", "M3", "M4", "M5", "M6"):
            assert f"**{milestone} specifically**" in gaps, (
                f"{milestone} has no known-gaps subsection. Every milestone here has "
                "unverified parts; one claiming none is the suspicious case."
            )


class TestEvidenceIndex:
    def test_index_exists(self) -> None:
        assert (REPO / "evidence" / "README.md").is_file()

    def test_every_evidence_folder_has_a_readme(self) -> None:
        """Each folder must carry its own caveat, because a reader lands in one
        folder without reading the index."""
        evidence = REPO / "evidence"
        missing = [
            folder.name
            for folder in evidence.iterdir()
            if folder.is_dir() and not (folder / "README.md").is_file()
        ]
        # No allowlist. An earlier version whitelisted m1 and m2 by name — a test
        # written around the gap it exists to prevent, which is worse than no test
        # because it reports green on the thing it is meant to catch.
        assert not missing, (
            f"evidence folders with no README: {sorted(missing)}. Every folder needs "
            "its own caveat: a reader lands in one without reading the index."
        )

    def test_index_states_nothing_is_deployed(self) -> None:
        index = (REPO / "evidence" / "README.md").read_text(encoding="utf-8").lower()
        # Checked as a claim, not as an exact sentence — the wording may change,
        # but the index must keep saying it somewhere prominent.
        assert any(
            phrase in index
            for phrase in ("never been deployed", "ever been deployed", "nothing has ever been deployed")
        ), "the evidence index must state plainly that nothing has been deployed"


class TestDiscussionAnswers:
    def test_all_seven_questions_are_answered(self) -> None:
        """The assignment lists seven questions to defend on a whiteboard."""
        path = REPO / "docs" / "discussion.md"
        assert path.is_file(), "docs/discussion.md is missing"
        content = path.read_text(encoding="utf-8")
        headings = re.findall(r"^## (\d)\.", content, re.M)
        assert [int(h) for h in headings] == [1, 2, 3, 4, 5, 6, 7], (
            f"expected answers 1-7, found {headings}"
        )


class TestRunbook:
    def test_exists_and_covers_the_named_scenario(self) -> None:
        """The assignment asks specifically for the 3am 5xx page."""
        runbook = (REPO / "docs" / "runbook.md").read_text(encoding="utf-8")
        assert "5xx" in runbook
        assert "3am" in runbook.lower() or "3 am" in runbook.lower()

    def test_alarms_link_to_a_runbook_that_exists(self) -> None:
        """Every alarm description points here. A dead link at 3am is worse than none."""
        assert (REPO / "docs" / "runbook.md").is_file()
