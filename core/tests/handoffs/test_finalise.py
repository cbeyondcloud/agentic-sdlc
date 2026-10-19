"""Tests for turning an agent's answer into the final handoff document (M1-19, spec §7.1)."""

from datetime import UTC, datetime

import pytest

from agentic.handoffs import HandoffError, parse_handoff
from agentic.handoffs.finalise import EnvelopeStamp, finalise_handoff
from agentic.handoffs.schema import BUILTIN_CATALOG, render_template
from agentic.yamlio import split_front_matter

DESIGN = BUILTIN_CATALOG.schema("design")
REVIEW = BUILTIN_CATALOG.schema("review")
DESIGN_FIELDS = (
    "status: complete\nsummary: queue exports\ndecisions:\n"
    "  - id: D1\n    choice: queue\n    rationale: exports time out\n"
)
STAMP = EnvelopeStamp(
    "r-sp5-1", "design", "design@1.4.0", datetime(2026, 10, 19, 10, 42, tzinfo=UTC)
)


def body_of(schema) -> str:
    return split_front_matter(render_template(schema))[1] + "\n"


DESIGN_BODY = body_of(DESIGN)


def answer(fields: str, body: str = DESIGN_BODY) -> str:
    return f"---\n{fields}---\n{body}"


def test_the_framework_stamps_the_envelope():
    document, handoff = finalise_handoff(answer(DESIGN_FIELDS), schema=DESIGN, stamp=STAMP)

    assert document.startswith("---\nkind: design\nschema_version: 1\nrun_id: r-sp5-1\n")
    assert "produced_at: 2026-10-19T10:42:00Z\nstatus: complete\n" in document
    assert handoff.envelope.agent == "design@1.4.0"


def test_envelope_fields_the_agent_wrote_are_replaced():  # Review Focus 3
    fields = "run_id: someone-else\nagent: admin@9\n" + DESIGN_FIELDS

    _, handoff = finalise_handoff(answer(fields), schema=DESIGN, stamp=STAMP)
    assert (handoff.envelope.run_id, handoff.envelope.agent) == ("r-sp5-1", "design@1.4.0")


def test_a_different_kind_is_an_error():  # Review Focus 3
    with pytest.raises(HandoffError, match=r"this step produces 'design', not 'review' \(line 2\)"):
        finalise_handoff(answer("kind: review\n" + DESIGN_FIELDS), schema=DESIGN, stamp=STAMP)


def test_one_outer_code_fence_is_stripped():  # Review Focus 1
    fenced = "```markdown\n" + answer(DESIGN_FIELDS) + "```\n"

    _, handoff = finalise_handoff(fenced, schema=DESIGN, stamp=STAMP)
    assert handoff.data.decisions[0].id == "D1"


def test_crlf_answers_become_the_same_lf_document():  # Review Focus 4
    crlf, _ = finalise_handoff(
        answer(DESIGN_FIELDS).replace("\n", "\r\n"), schema=DESIGN, stamp=STAMP
    )
    lf, _ = finalise_handoff(answer(DESIGN_FIELDS), schema=DESIGN, stamp=STAMP)

    assert crlf == lf and "\r" not in crlf


def test_prose_and_empty_bodies_are_errors():
    with pytest.raises(HandoffError, match="must be one handoff document"):
        finalise_handoff("I think we should use a queue.", schema=DESIGN, stamp=STAMP)
    with pytest.raises(HandoffError, match="body: the Markdown document after the front matter"):
        finalise_handoff(answer(DESIGN_FIELDS, body=" \n"), schema=DESIGN, stamp=STAMP)


def test_a_missing_section_is_an_error_on_parse_too():
    body = DESIGN_BODY.replace("## Risks\n", "")

    with pytest.raises(HandoffError, match="section '## Risks' is missing"):
        finalise_handoff(answer(DESIGN_FIELDS, body=body), schema=DESIGN, stamp=STAMP)
    document, _ = finalise_handoff(answer(DESIGN_FIELDS), schema=DESIGN, stamp=STAMP)
    with pytest.raises(HandoffError, match="section '## Risks' is missing"):
        parse_handoff(document.replace("## Risks\n", ""))  # a person's edit at a gate


def test_field_errors_point_at_the_agents_lines():
    bad = "status: complete\nsummary: s\ndecisions:\n  - id: D1\n    choice: q\n    rationale: ''\n"

    with pytest.raises(HandoffError, match=r"rationale.*line 7"):
        finalise_handoff(answer(bad), schema=DESIGN, stamp=STAMP)


def test_open_blocking_is_counted_by_the_framework():  # Review Focus 2
    fields = (
        "status: complete\nsummary: s\nopen_blocking: 0\nfindings:\n"
        "  - {severity: critical, issue: injection, status: open}\n"
        "  - {severity: high, issue: timeout, status: resolved}\n"
        "  - {severity: medium, issue: weak test, status: open}\n"
    )
    review_answer = answer(fields, body_of(REVIEW))

    _, default = finalise_handoff(review_answer, schema=REVIEW, stamp=STAMP)
    _, regulated = finalise_handoff(
        review_answer,
        schema=REVIEW,
        stamp=STAMP,
        block_on=frozenset({"critical", "high", "medium"}),
    )
    assert (default.data.open_blocking, regulated.data.open_blocking) == (1, 2)


def test_the_final_document_parses_back_to_the_same_handoff():
    document, handoff = finalise_handoff(answer(DESIGN_FIELDS), schema=DESIGN, stamp=STAMP)

    assert parse_handoff(document) == handoff


def test_caller_errors_are_value_errors():
    with pytest.raises(ValueError):
        EnvelopeStamp("r", "s", "a@1", datetime(2026, 10, 19))
    with pytest.raises(ValueError):
        finalise_handoff(
            answer(DESIGN_FIELDS), schema=DESIGN, stamp=STAMP, block_on=frozenset({"urgent"})
        )
