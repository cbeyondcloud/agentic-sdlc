"""Feedback records and the journal events that carry them (M3-11, M3-12)."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agentic.learning.records import (
    ANALYSED,
    CAPTURED,
    CORRECTED,
    MAX_DIFF,
    MAX_TEXT,
    Attribution,
    FeedbackRecord,
    Learner,
    analysed_fields,
    captured_fields,
    corrected_fields,
    feedback_id,
    fold,
)

AT = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


def record(**extra) -> FeedbackRecord:
    fields = {
        "id": feedback_id("r-1", "gate-rejection", "gate:design-review:reject:abc"),
        "signal": "gate-rejection",
        "run_id": "r-1",
        "actor": "lead@example.test",
        "given_at": AT,
        "text": "Use the existing queue, don't add a new one.",
        "source": "gate:design-review:reject:abc",
        "gate": "design-review",
        "kind": "design",
        "agent": "design@1.0.0",
    }
    return FeedbackRecord(**{**fields, **extra})


def test_same_source_same_id():
    one = feedback_id("r-1", "pr-review", "pr:github.com/acme/app#7:comment:11")
    assert one == feedback_id("r-1", "pr-review", "pr:github.com/acme/app#7:comment:11")
    assert one != feedback_id("r-1", "pr-review", "pr:github.com/acme/app#7:comment:12")
    assert one != feedback_id("r-2", "pr-review", "pr:github.com/acme/app#7:comment:11")
    assert one.startswith("fb-") and len(one) == 23


def test_long_text_and_diff_are_capped():
    found = record(text="x" * (MAX_TEXT + 50), diff="y" * (MAX_DIFF + 50))
    assert len(found.text) <= MAX_TEXT and found.text.endswith("characters]")
    assert len(found.diff) <= MAX_DIFF and found.diff.endswith("characters]")


def test_unknown_signal_and_extra_field_refused():
    with pytest.raises(ValidationError):
        record(signal="qa-miss")
    with pytest.raises(ValidationError):
        record(prompt="nope")


def test_learner_names_an_agent_with_a_confidence():
    with pytest.raises(ValidationError):
        Learner(agent="Developer Agent", confidence=0.5)
    with pytest.raises(ValidationError):
        Learner(agent="developer", confidence=1.5)


def test_fold_applies_analysis_then_correction():
    rec = record()
    rules = Attribution(learners=(Learner(agent="design", confidence=1.0),), method="rules")
    analyst = Attribution(learners=(Learner(agent="developer", confidence=0.7),), method="analyst")
    human = Attribution(
        learners=(Learner(agent="business-analyst", confidence=1.0),),
        method="human",
        reason="it was a requirements gap",
    )
    entries = [
        {"event": CAPTURED, **captured_fields(rec, rules, "use the existing queue")},
        {"event": ANALYSED, **analysed_fields(rec.id, analyst, "reuse the queue")},
        {"event": CORRECTED, **corrected_fields(rec.id, human, "lead@example.test")},
        {"event": ANALYSED, **analysed_fields("fb-unknown", analyst, "ignored")},
    ]
    state = fold(entries)
    assert list(state) == [rec.id]
    found = state[rec.id]
    assert found.record == rec
    assert found.lesson == "reuse the queue"
    assert found.attribution == human
    assert found.corrected_by == "lead@example.test"


def test_analysis_never_replaces_a_correction():
    rec = record()
    human = Attribution(learners=(Learner(agent="qa", confidence=1.0),), method="human")
    analyst = Attribution(learners=(Learner(agent="developer", confidence=0.7),), method="analyst")
    entries = [
        {"event": CAPTURED, **captured_fields(rec, None, "x")},
        {"event": CORRECTED, **corrected_fields(rec.id, human, "lead@example.test")},
        {"event": ANALYSED, **analysed_fields(rec.id, analyst, "y")},
    ]
    found = fold(entries)[rec.id]
    assert found.attribution == human and found.lesson == "y"
