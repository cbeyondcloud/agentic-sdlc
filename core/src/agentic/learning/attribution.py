"""Which agents learn from a correction: the rules (M3-12, spec §24 "Attribute").

| Correction                                                    | Agents that learn            |
|---------------------------------------------------------------|------------------------------|
| a gate rejected or edited a handoff                           | the agent that wrote it      |
|   (requirements -> Business Analyst, design -> Design, a      |                              |
|   custom agent's handoff -> that agent)                       |                              |
| a review-agent miss                                           | the Code Review agent        |
| a review comment about missing or weak tests                  | QA (+ Developer if the run's |
|                                                               | developer reported no tests) |
| a review comment about architecture or dependencies           | Design                       |
| a review comment about wrong behaviour against the intent     | Business Analyst             |
| any other code change requested in review (an inline comment, | Developer (made it) and Code |
|   or a "changes requested" review)                            | Review (missed it)           |

A rule decides with confidence 1. What no rule decides (a general comment with no code
location, a rejection whose handoff is unknown) returns None: the Learning Analyst
attributes it with a confidence, and a person may correct either.

Agents are named as the run named them: `RunAgents` maps each handoff kind to the agent
that wrote it in the run, so a team's own developer agent learns, not the library's.
"""

import re
from collections.abc import Mapping

from agentic.learning.records import Attribution, FeedbackRecord, Learner

#: The library agent of each handoff kind, when the run does not say (M1-60).
LIBRARY_AGENTS = {
    "requirements": "business-analyst",
    "design": "design",
    "dev-report": "developer",
    "review": "code-review",
    "test-report": "qa",
}

_TESTS = re.compile(
    r"\b(tests?|tested|untested|testing|test cases?|coverage|assertions?|unit tests?)\b", re.I
)
_ARCHITECTURE = re.compile(
    r"\b(architecture|architectural|dependency|dependencies|layering|layers?|coupling|"
    r"module boundar(y|ies)|service boundar(y|ies)|design)\b",
    re.I,
)
_INTENT = re.compile(
    r"\b(requirements?|acceptance criteri(a|on)|not what (was|we) asked|intended behaviou?r|"
    r"expected behaviou?r|user story|specification)\b",
    re.I,
)


class RunAgents:
    """The agent of each handoff kind in one run (kind -> agent name)."""

    def __init__(self, by_kind: Mapping[str, str] | None = None) -> None:
        self.by_kind = {**LIBRARY_AGENTS, **dict(by_kind or {})}

    def for_kind(self, kind: str) -> str:
        return self.by_kind.get(kind, LIBRARY_AGENTS.get(kind, kind))

    def names(self) -> list[str]:
        return sorted(set(self.by_kind.values()))


def _decided(reason: str, *agents: str) -> Attribution:
    unique = list(dict.fromkeys(agents))
    return Attribution(
        learners=tuple(Learner(agent=agent, confidence=1.0) for agent in unique),
        method="rules",
        reason=reason,
    )


def rules(record: FeedbackRecord, agents: RunAgents) -> Attribution | None:
    """The rules' attribution, or None when they do not decide."""
    if record.signal in ("gate-rejection", "gate-edit"):
        what = "rejected" if record.signal == "gate-rejection" else "edited"
        if record.agent_name:
            return _decided(
                f"its {record.kind or 'handoff'} was {what} at a gate", record.agent_name
            )
        if record.kind:
            return _decided(
                f"the {record.kind} handoff was {what} at a gate", agents.for_kind(record.kind)
            )
        return None
    if record.signal == "review-miss":
        reviewer = record.agent_name or agents.for_kind("review")
        return _decided("a person asked for a change on lines its review passed", reviewer)
    text = record.text
    if _TESTS.search(text):
        learners = [agents.for_kind("test-report")]
        if record.tdd_skipped:
            learners.append(agents.for_kind("dev-report"))
        return _decided("a reviewer flagged missing or weak tests", *learners)
    if _ARCHITECTURE.search(text):
        return _decided("a reviewer cited architecture or dependencies", agents.for_kind("design"))
    if _INTENT.search(text):
        return _decided(
            "a reviewer said the behaviour is not what was intended",
            agents.for_kind("requirements"),
        )
    if record.path or record.review_state == "changes_requested":
        return _decided(
            "a code change was requested in review: the developer made it, the review missed it",
            agents.for_kind("dev-report"),
            agents.for_kind("review"),
        )
    return None


__all__ = ["LIBRARY_AGENTS", "RunAgents", "rules"]
