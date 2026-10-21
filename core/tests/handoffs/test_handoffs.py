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
from agentic.handoffs.schema import BUILTIN_CATALOG, render_template
from agentic.yamlio import split_front_matter

ENVELOPE = (
    "schema_version: 1\nrun_id: r-1\nstep: s1\nagent: a@1.0.0\n"
    "produced_at: 2026-10-16T10:00:00Z\nstatus: complete\nsummary: s\n"
)


def handoff(kind: str, fields: str, body: str | None = None) -> str:
    """Build a handoff document: envelope + kind fields in front matter, then the body
    (by default the kind's template body, so every required section is present)."""
    if body is None:
        known = kind in BUILTIN_CATALOG.schemas
        body = (
            split_front_matter(render_template(BUILTIN_CATALOG.schema(kind)))[1] if known else "B."
        )
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
    assert result.body.startswith("# Design: async exports")


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


# --- test-report: judged against the baseline taken at intake (Zyroflo P5) ---

BASELINE_RED = "baseline_failures: [tests/test_legacy.py::test_flaky_clock]\n"


def test_pass_with_only_baseline_failures_is_valid():
    """No NEW failures is the bar, not zero failures: a suite already red at intake
    may still pass when every failure is named and was already failing."""
    fields = (
        "verdict: pass\ntests: {passed: 40, failed: 1}\n"
        "failures: [tests/test_legacy.py::test_flaky_clock]\n" + BASELINE_RED
    )
    result = parse_handoff(handoff("test-report", fields))

    assert result.data.failures == result.data.baseline_failures


def test_pass_with_a_new_failure_is_refused():
    fields = (
        "verdict: pass\ntests: {passed: 39, failed: 2}\n"
        "failures: [tests/test_legacy.py::test_flaky_clock, tests/test_export.py::test_csv]\n"
        + BASELINE_RED
    )
    with pytest.raises(HandoffError, match="tests/test_export.py::test_csv"):
        parse_handoff(handoff("test-report", fields))


def test_pass_with_unnamed_failures_is_refused():
    fields = "verdict: pass\ntests: {passed: 40, failed: 1}\n" + BASELINE_RED
    with pytest.raises(HandoffError, match="1 test"):
        parse_handoff(handoff("test-report", fields))


def test_naming_one_baseline_failure_twice_does_not_cover_a_second_failure():
    fields = (
        "verdict: pass\ntests: {passed: 39, failed: 2}\n"
        "failures: [tests/test_legacy.py::test_flaky_clock,\n"
        "           tests/test_legacy.py::test_flaky_clock]\n" + BASELINE_RED
    )
    with pytest.raises(HandoffError, match="2 test"):
        parse_handoff(handoff("test-report", fields))


@pytest.mark.parametrize(
    ("counts", "failures"),
    [
        (
            "{passed: 40, failed: 1}",
            "[tests/test_legacy.py::test_flaky_clock, tests/test_legacy.py::test_flaky_clock]",
        ),  # one failure named twice
        ("{passed: 40, failed: 0}", "[tests/test_legacy.py::test_flaky_clock]"),  # 0 vs 1
        ("{passed: 40, failed: 2}", "[tests/test_legacy.py::test_flaky_clock]"),  # 2 vs 1
    ],
    ids=["duplicate", "named-but-none-failed", "fewer-names-than-failures"],
)
def test_a_pass_must_name_each_failure_exactly_once(counts: str, failures: str):  # DF-230
    fields = f"verdict: pass\ntests: {counts}\nfailures: {failures}\n" + BASELINE_RED
    with pytest.raises(HandoffError, match="once"):
        parse_handoff(handoff("test-report", fields))


def test_a_fail_verdict_needs_no_names():
    result = parse_handoff(handoff("test-report", "verdict: fail\ntests: {passed: 1, failed: 3}\n"))

    assert result.data.failures == [] and result.data.baseline_failures == []


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

    with pytest.raises(HandoffError) as error:
        parse_handoff(text)

    # Line 1 is "---"; the broken list starts on line 11 of the document.
    assert "started at line 11" in str(error.value)


def test_missing_front_matter_rejected():
    with pytest.raises(HandoffError) as error:
        parse_handoff("Just some text without a header.\n")

    assert "front matter" in str(error.value)


# --- compatibility: does every step receive the documents it needs? ---


def make_agent(name: str, consumes: list[str], produces: str | None) -> AgentDefinition:
    return AgentDefinition(
        name=name,
        type="general",
        version="1.0.0",
        description=name,
        instructions="Do it.",
        consumes=consumes,
        produces=produces,
    )


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
        {
            "loop": {
                "id": "build-review",
                "max_iterations": 3,
                "until": "x",
                "steps": [
                    {"id": "develop", "agent": "developer"},
                    {"id": "review", "agent": "code-review"},
                ],
            }
        },
        {"id": "qa", "agent": "qa"},
    ]

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


def test_non_string_kind_rejected_cleanly():
    # A person editing `kind:` into a list used to crash with a raw TypeError.
    with pytest.raises(HandoffError) as error:
        parse_handoff(handoff("[a, b]", "open_blocking: 0\n"))

    assert "kind" in str(error.value)


def test_field_error_names_the_line():
    # ---=1, kind=2, schema_version=3, run_id=4, step=5, agent=6, produced_at=7, status=8
    text = handoff("review", "open_blocking: 0\n").replace("status: complete", "status: finished")

    with pytest.raises(HandoffError) as error:
        parse_handoff(text)

    assert "status:" in str(error.value)
    assert "(line 8)" in str(error.value)


def test_unclosed_quote_points_at_its_line():
    text = handoff("review", "open_blocking: 0\nfindings: []\n").replace(
        "run_id: r-1", 'run_id: "r-1'
    )

    with pytest.raises(HandoffError) as error:
        parse_handoff(text)

    assert "started at line 4" in str(error.value)  # run_id is on line 4


def test_compat_uses_handoffs_inherited_from_the_agent_type(fixtures: Path):
    # The developer file below sets no consumes/produces: they come from its type (D35).
    from agentic.policy import ResolveContext, load_policy, resolve_agent

    policy = load_policy(fixtures / "policy" / "policy")
    bare_developer = AgentDefinition(
        name="developer", type="developer", version="1.0.0", description="d", instructions="x"
    )
    resolved, violations = resolve_agent(bare_developer, policy, ResolveContext())
    assert violations == []

    steps = [{"id": "develop", "agent": "developer"}]  # nothing produces "design" first
    problems = check_compatibility(pipeline(steps), {"developer": resolved})

    assert [p.requested for p in problems] == ["design", "review"]
