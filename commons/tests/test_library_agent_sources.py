"""The built-in agents' instructions are adapted from Zyroflo's lifecycle SOPs and review
rubric (M1-60, spec §5.4, D34). Zyroflo stays the source: these tests read it live through
`sdlc_commons.lifecycle` and fail when the adaptation drifts from it."""

import re
from pathlib import Path

import pytest

from sdlc_commons import lifecycle

AGENTS = Path(__file__).resolve().parents[2] / "library" / "agents"
#: agent file -> the Zyroflo stages its instructions are adapted from (spec §5.4)
SOURCES = {
    "business-analyst": ["P0", "P1"],
    "design": ["P2", "P3"],
    "developer": ["P4"],
    "code-review": ["P6"],
    "qa": ["P5", "P10"],
    "qa-baseline": ["P0", "P5"],
}
_ROW = re.compile(r"^\|\s*\d+\s*\|\s*(?P<name>[^|]+?)\s*\|\s*(?P<weight>\d+(?:\.\d+)?)\s*\|")


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_source_stages_exist(name: str):
    assert (AGENTS / f"{name}.md").is_file()
    for phase in SOURCES[name]:
        assert lifecycle.stage(phase).instructions.strip(), phase


def test_code_review_rubric_matches_zyroflo():
    """Same categories, same weights (summing to 10) as Zyroflo's review-rubric.md."""
    text = (AGENTS / "code-review.md").read_text(encoding="utf-8")
    table = {m["name"]: float(m["weight"]) for m in map(_ROW.match, text.splitlines()) if m}
    expected = {category.name: category.weight for category in lifecycle.review_rubric()}
    assert table == expected
    assert sum(table.values()) == pytest.approx(10)


def test_code_review_deductions_and_critical_cap_match_zyroflo():  # DF-235
    """Changing -0.5/-0.2/-0.05 or dropping the 8.0 critical cap in code-review.md
    passed CI: they are read from review-rubric.md too."""
    text = " ".join((AGENTS / "code-review.md").read_text(encoding="utf-8").split())
    rubric = lifecycle.review_deductions()
    assert set(rubric.per_finding) == {"high", "medium", "low"}
    for severity, amount in rubric.per_finding.items():
        assert f"{severity} -{amount:g}" in text, severity
    assert f"any critical caps the final score at {rubric.critical_cap:.1f}" in text


def test_the_rubric_deductions_are_read_from_zyroflo():  # DF-235
    rubric = lifecycle.review_deductions()
    assert rubric.per_finding == {"high": 0.5, "medium": 0.2, "low": 0.05}
    assert rubric.critical_cap == 8.0


def test_sop_rules_the_agents_rely_on_still_hold():
    """The rules adapted into the agents are still Zyroflo's rules."""
    assert "EARS" in lifecycle.stage("P1").instructions  # BA: EARS criteria
    assert "no NEW failures" in lifecycle.stage("P5").instructions  # QA: the baseline bar
    review = lifecycle.stage("P6").instructions
    assert "**The exit is categorical.**" in review  # Code Review: score never gates
