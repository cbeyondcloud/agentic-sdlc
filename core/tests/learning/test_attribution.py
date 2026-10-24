"""Attribution rules (M3-12, spec §24 "Attribute"): which agents learn from a correction.
What the rules cannot decide is left to the Learning Analyst (None here)."""

from datetime import UTC, datetime

from agentic.learning.attribution import RunAgents, rules
from agentic.learning.records import FeedbackRecord, feedback_id

AT = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
AGENTS = RunAgents({"dev-report": "payments-dev"})  # a team's own developer agent


def rec(signal="pr-review", text="", **extra) -> FeedbackRecord:
    source = extra.pop("source", f"src-{signal}-{text}-{extra}")
    return FeedbackRecord(
        id=feedback_id("r-1", signal, source),
        signal=signal,
        run_id="r-1",
        actor="alice",
        given_at=AT,
        text=text,
        source=source,
        **extra,
    )


def learners(attribution) -> list[tuple[str, float]]:
    return [(learner.agent, learner.confidence) for learner in attribution.learners]


def test_pr_code_comment_teaches_developer_and_code_review():
    found = rules(
        rec(text="Wrap this call in our with_retry() helper.", path="billing/export.py", line=42),
        AGENTS,
    )
    assert learners(found) == [("payments-dev", 1.0), ("code-review", 1.0)]
    assert found.method == "rules" and found.reason


def test_changes_requested_review_body_is_a_code_change_request():
    found = rules(
        rec(text="Please use the retry helper.", review_state="changes_requested"), AGENTS
    )
    assert [learner.agent for learner in found.learners] == ["payments-dev", "code-review"]


def test_requirements_edit_teaches_business_analyst():
    found = rules(
        rec(
            "gate-edit",
            "hourly, not nightly",
            kind="requirements",
            agent="business-analyst@1.0.0",
            gate="requirements-review",
        ),
        AGENTS,
    )
    assert learners(found) == [("business-analyst", 1.0)]


def test_requirements_rejection_and_intent_comment_teach_business_analyst():
    rejected = rules(rec("gate-rejection", "missing a criterion", kind="requirements"), AGENTS)
    assert [learner.agent for learner in rejected.learners] == ["business-analyst"]
    intent = rules(
        rec(text="This is not what the acceptance criteria asked for.", path="a.py", line=3),
        AGENTS,
    )
    assert [learner.agent for learner in intent.learners] == ["business-analyst"]


def test_design_reject_and_architecture_comment_teach_design():
    rejected = rules(
        rec("gate-rejection", "use the existing queue", kind="design", agent="design@1.0.0"),
        AGENTS,
    )
    assert [learner.agent for learner in rejected.learners] == ["design"]
    comment = rules(
        rec(text="This adds a dependency on the billing service; keep the layering.", path="a.py"),
        AGENTS,
    )
    assert [learner.agent for learner in comment.learners] == ["design"]


def test_missing_tests_teach_qa_and_developer_when_tdd_skipped():
    plain = rules(rec(text="There is no test for the empty export.", path="a.py", line=8), AGENTS)
    assert [learner.agent for learner in plain.learners] == ["qa"]
    skipped = rules(
        rec(text="No tests cover this branch.", path="a.py", line=8, tdd_skipped=True), AGENTS
    )
    assert [learner.agent for learner in skipped.learners] == ["qa", "payments-dev"]


def test_custom_agent_handoff_teaches_that_agent():
    found = rules(
        rec("gate-edit", "fix the wording", kind="docs", agent="docs-writer@0.3.0"), AGENTS
    )
    assert learners(found) == [("docs-writer", 1.0)]


def test_review_miss_teaches_code_review():
    found = rules(
        rec("review-miss", "Wrap this call.", path="a.py", line=4, agent="strict-review@2.0.0"),
        AGENTS,
    )
    assert learners(found) == [("strict-review", 1.0)]


def test_general_comment_is_left_to_the_analyst():
    assert rules(rec(text="Hmm, interesting approach.", review_state="commented"), AGENTS) is None
    assert rules(rec("gate-rejection", "not like this"), AGENTS) is None  # no handoff known
