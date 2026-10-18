"""Tests for handoff documents: envelope, the five kinds, parsing, and pipeline compatibility."""

from pathlib import Path

import pytest

from agentic.definitions import AgentDefinition, PipelineDefinition
from agentic.handoffs import (
    DesignData,
    HandoffError,
    ReviewData,
    check_compatibility,
    parse_handoff,
)

ENVELOPE = (
    "schema_version: 1\nrun_id: r-1\nstep: s1\nagent: a@1.0.0\n"
    "produced_at: 2026-10-16T10:00:00Z\nstatus: complete\n"
)


def handoff(kind: str, fields: str, body: str = "Body.") -> str:
    """Build a handoff document: envelope + kind fields in front matter, then the body."""
    return f"---\nkind: {kind}\n{ENVELOPE}{fields}---\n{body}\n"


# --- parsing ---


def test_parses_design_example(fixtures: Path):
    text = (fixtures / "handoffs" / "design.md").read_text(encoding="utf-8")

    result = parse_handoff(text)

    assert result.envelope.kind == "design"
    assert result.envelope.agent == "design@1.4.0"
    assert isinstance(result.data, DesignData)
    assert result.data.decisions[0].rationale == "Export latency exceeds API timeout"
    assert result.data.impact.confidence == "candidate"
    assert result.body.startswith("## Design: async exports")


SAMPLES = {
    "requirements": (
        "size: small\nitems:\n  - id: R1\n    statement: Users can export invoices\n"
        "    acceptance_criteria:\n"
        "      - WHEN a user requests an export THE system SHALL return a job id\n"
    ),
    "design": "decisions:\n  - id: D1\n    choice: Use a queue\n    rationale: Slow exports\n",
    "dev-report": (
        "commits: [abc1234]\nfiles_changed: [src/export.py]\ntests_run: {passed: 12, failed: 0}\n"
    ),
    "review": "open_blocking: 0\nscore: 8.5\nfindings: []\n",
    "test-report": "verdict: pass\ntests: {passed: 40, failed: 0}\n",
}


@pytest.mark.parametrize("kind", sorted(SAMPLES))
def test_each_kind_parses(kind: str):
    result = parse_handoff(handoff(kind, SAMPLES[kind]))

    assert result.envelope.kind == kind


def test_missing_rationale_rejected():
    text = handoff("design", "decisions:\n  - id: D1\n    choice: Use a queue\n")

    with pytest.raises(HandoffError) as error:
        parse_handoff(text)

    assert "rationale" in str(error.value)


def test_unknown_kind_rejected():
    with pytest.raises(HandoffError) as error:
        parse_handoff(handoff("poem", "lines: 4\n"))

    assert "poem" in str(error.value)


def test_unknown_field_rejected():
    with pytest.raises(HandoffError) as error:
        parse_handoff(handoff("review", SAMPLES["review"] + "mood: grumpy\n"))

    assert "mood" in str(error.value)


def test_unsupported_schema_version_rejected():
    text = handoff("review", SAMPLES["review"]).replace("schema_version: 1", "schema_version: 7")

    with pytest.raises(HandoffError) as error:
        parse_handoff(text)

    assert "schema_version" in str(error.value)


def test_negative_open_blocking_rejected():
    with pytest.raises(HandoffError) as error:
        parse_handoff(handoff("review", "open_blocking: -1\nfindings: []\n"))

    assert "open_blocking" in str(error.value)


def test_review_values_available_for_pipeline_conditions():
    result = parse_handoff(handoff("review", SAMPLES["review"]))

    assert isinstance(result.data, ReviewData)
    assert result.data.open_blocking == 0  # read by `until: review.open_blocking == 0`
    assert result.data.score == 8.5  # reported, never the gate


def test_broken_front_matter_reports_line():
    text = handoff("review", "open_blocking: 0\nfindings: [unclosed\n")
    original = text

    with pytest.raises(HandoffError) as error:
        parse_handoff(text)

    # Line 1 is "---"; the broken list starts on line 10 of the document.
    assert "line 10" in str(error.value)
    assert text == original  # the human's edit is never altered


def test_missing_front_matter_rejected():
    with pytest.raises(HandoffError) as error:
        parse_handoff("Just some text without a header.\n")

    assert "front matter" in str(error.value)


# --- compatibility: does every step receive the documents it needs? ---


def make_agent(name: str, consumes: list[str], produces: str | None) -> AgentDefinition:
    return AgentDefinition(
        name=name, version="1.0.0", description=name, instructions="Do it.",
        consumes=consumes, produces=produces,
    )  # fmt: skip


AGENTS = {
    "business-analyst": make_agent("business-analyst", [], "requirements"),
    "design": make_agent("design", ["requirements"], "design"),
    "developer": make_agent("developer", ["design", "review"], "dev-report"),
    "code-review": make_agent("code-review", ["dev-report"], "review"),
    "qa": make_agent("qa", ["requirements", "dev-report"], "test-report"),
}


def pipeline(steps: list[dict]) -> PipelineDefinition:
    return PipelineDefinition.model_validate({"name": "p", "version": "1.0.0", "steps": steps})


def test_compat_allows_loop_feedback():
    # developer consumes "review", which the loop's later step produces (previous iteration).
    steps = [
        {"id": "requirements", "agent": "business-analyst"},
        {"id": "design", "agent": "design"},
        {"loop": {"id": "build-review", "max_iterations": 3, "until": "x", "steps": [
            {"id": "develop", "agent": "developer"},
            {"id": "review", "agent": "code-review"},
        ]}},
        {"id": "qa", "agent": "qa"},
    ]  # fmt: skip

    assert check_compatibility(pipeline(steps), AGENTS) == []


def test_compat_flags_unproduced_input():
    steps = [{"id": "requirements", "agent": "business-analyst"}, {"id": "qa", "agent": "qa"}]

    violations = check_compatibility(pipeline(steps), AGENTS)

    assert len(violations) == 1
    assert violations[0].requested == "dev-report"
    assert "qa" in violations[0].reason


def test_compat_reports_unknown_agent():
    steps = [{"id": "s1", "agent": "ghost-writer"}]

    violations = check_compatibility(pipeline(steps), AGENTS)

    assert [v.field for v in violations] == ["agent"]
    assert "ghost-writer" in violations[0].reason
