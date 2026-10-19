"""Tests for the handoff base template and the built-in kinds inheriting it (M1-19, D37)."""

from importlib.resources import files

import pytest
from pydantic import ValidationError

from agentic.handoffs import HANDOFF_KINDS, TestReportData
from agentic.handoffs.schema import (
    BUILTIN_CATALOG,
    HandoffBase,
    HandoffCatalog,
    render_template,
)
from agentic.yamlio import load_yaml, split_front_matter


def extra_for(kind: str) -> dict:
    return {"open_blocking": 0} if kind == "review" else {}  # computed by the framework


def load_kind_file(kind: str) -> str:
    return (files("agentic.handoffs") / "templates" / f"{kind}.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", list(HANDOFF_KINDS))
def test_every_kind_inherits_the_base(kind):
    schema = BUILTIN_CATALOG.schema(kind)

    assert issubclass(schema.model, HandoffBase)
    assert schema.sections[0].heading == "Summary"
    assert schema.sections[-1].heading == "Open questions"


@pytest.mark.parametrize("kind", list(HANDOFF_KINDS))
def test_every_builtin_template_is_valid_for_its_model(kind):
    schema = BUILTIN_CATALOG.schema(kind)
    header, body = split_front_matter(render_template(schema))
    fields, _ = load_yaml(header)

    schema.model.model_validate(
        {k: v for k, v in fields.items() if k != "status"} | extra_for(kind)
    )
    headings = [line for line in body.splitlines() if line.startswith("## ")]
    assert headings == [f"## {s.heading}" for s in schema.sections]
    assert fields["status"] == "complete"


def test_kind_files_do_not_repeat_the_base():
    for kind in HANDOFF_KINDS:
        text = load_kind_file(kind)
        assert "## Summary" not in text and "summary:" not in text


def test_the_design_sections_follow_madr():
    headings = [s.heading for s in BUILTIN_CATALOG.schema("design").sections]

    assert headings[1:5] == [
        "Context and problem statement",
        "Decision drivers",
        "Considered options",
        "Decision outcome",
    ]


def test_every_section_has_guidance():
    for kind in HANDOFF_KINDS:
        assert all(s.guidance.strip() for s in BUILTIN_CATALOG.schema(kind).sections)


def test_a_passing_test_report_with_failures_is_invalid():
    with pytest.raises(ValidationError, match="verdict"):
        TestReportData.model_validate(
            {"summary": "s", "verdict": "pass", "tests": {"passed": 1, "failed": 1}}
        )
    with pytest.raises(ValidationError, match="AC-2"):
        TestReportData.model_validate(
            {
                "summary": "s",
                "verdict": "pass",
                "tests": {"passed": 2, "failed": 0},
                "acceptance_results": [{"criterion_id": "AC-2", "passed": False}],
            }
        )


def test_the_catalog_digest_changes_with_any_schema():
    assert BUILTIN_CATALOG.digest.startswith("sha256:")
    smaller = HandoffCatalog({k: s for k, s in BUILTIN_CATALOG.schemas.items() if k != "review"})

    assert smaller.digest != BUILTIN_CATALOG.digest


def test_an_unknown_kind_names_the_known_ones():
    with pytest.raises(ValueError, match="requirements"):
        BUILTIN_CATALOG.schema("poem")
