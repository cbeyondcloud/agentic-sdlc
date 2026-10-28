"""The built-in Learning Analyst (M3-12, spec §24 "Attribute" and "Detect repeats").

The analyst is an ordinary library agent (`library/agents/learning-analyst.md`, agent
type `learning-analyst`, tier `fast`, no tools) run through any `AgentRunner` - in
production the native backend's, so every call goes through the gateway, is metered
against a budget and secret-scanned like any step. It is asked about one record at a
time and answers with one JSON object, checked by `AnalystContract` (an output contract:
an invalid answer goes back once with its errors, then the call is refused):

    {"learners": [{"agent": "<candidate>", "confidence": 0.0-1.0}, ...],
     "lesson": "<one line, at most 200 characters>",
     "reason": "<why, in a sentence>"}

`analyse` puts the rules first: when they decide, the analyst only writes the lesson;
when they do not, its learners and confidences are the attribution (method `analyst`).
Without an analyst, or when it fails, the rules' answer (or None) stands and the lesson
is `heuristic_lesson`'s. The record is shown to the model as data, fenced.
"""

import hashlib
import json
import logging
import re
from collections.abc import Callable, Sequence
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from agentic.agents.runner import AgentRunner, AgentTask
from agentic.learning.attribution import RunAgents, rules
from agentic.learning.lessons import heuristic_lesson
from agentic.learning.records import MAX_LESSON, Attribution, FeedbackRecord, Learner
from agentic.security.untrusted import untrusted

_LOG = logging.getLogger("agentic.learning")
_FENCE = re.compile(r"^```[a-z]*\s*\n(?P<body>.*)\n```\s*$", re.DOTALL)
PROMPT = "Which of the candidate agents should learn from this correction, and what is the lesson?"


class AnalystError(Exception):
    """The analyst gave no valid verdict (refused twice, stopped, or failed)."""


class AnalystVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    learners: list[Learner] = Field(min_length=1, max_length=4)
    lesson: Annotated[str, StringConstraints(min_length=1, max_length=MAX_LESSON)]
    reason: Annotated[str, StringConstraints(max_length=500)] = ""


class AnalystContract:
    """The analyst's output contract: one JSON verdict naming only `candidates`."""

    def __init__(self, candidates: Sequence[str]) -> None:
        self.candidates = list(dict.fromkeys(candidates))

    @property
    def instructions(self) -> str:
        return (
            "Answer with one JSON object and nothing else:\n"
            '{"learners": [{"agent": "<name>", "confidence": <0..1>}], '
            '"lesson": "<one line, at most 200 characters>", "reason": "<one sentence>"}\n'
            f"Candidate agents (name only these): {', '.join(self.candidates)}."
        )

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.instructions.encode("utf-8")).hexdigest()

    def check(self, text: str) -> tuple[str | None, list[str]]:
        body = text.strip()
        if fenced := _FENCE.match(body):
            body = fenced.group("body").strip()
        try:
            verdict = AnalystVerdict.model_validate(json.loads(body))
        except ValueError as error:  # bad JSON, or a ValidationError
            if isinstance(error, ValidationError):
                errors = [
                    f"{'.'.join(str(p) for p in item['loc'])}: {item['msg']}"
                    for item in error.errors()
                ]
                return None, errors
            return None, [f"the answer is not one JSON object ({error})"]
        problems = [
            f"agent {learner.agent!r} is not one of the candidates"
            for learner in verdict.learners
            if learner.agent not in self.candidates
        ]
        if "\n" in verdict.lesson or "\r" in verdict.lesson:
            problems.append("lesson: write it on one line")
        if problems:
            return None, problems
        return verdict.model_dump_json(), []


def describe(record: FeedbackRecord) -> str:
    """The record as the analyst sees it: the facts, then the person's words and diff."""
    facts = {
        "signal": record.signal,
        "handoff kind": record.kind,
        "agent whose output was corrected": record.agent,
        "gate": record.gate,
        "file": record.path,
        "line": record.line,
        "review state": record.review_state,
        "developer reported no tests": record.tdd_skipped,
    }
    lines = [f"{name}: {value}" for name, value in facts.items() if value not in (None, "")]
    lines += ["", "What the person said:", record.text or "(nothing)"]
    if record.diff:
        lines += ["", "Their edit (unified diff):", record.diff]
    return "\n".join(lines)


class LearningAnalyst:
    """Runs the library's Learning Analyst: `runner_for(contract)` builds the runner for
    one call (the CLI's native backend; a scripted model in tests)."""

    def __init__(
        self, runner_for: Callable[[AnalystContract], AgentRunner], instructions: str
    ) -> None:
        self.runner_for, self.instructions = runner_for, instructions

    async def verdict(self, record: FeedbackRecord, candidates: Sequence[str]) -> AnalystVerdict:
        contract = AnalystContract(candidates)
        runner = self.runner_for(contract)
        task = AgentTask(
            instructions=self.instructions,
            inputs={},
            prompt=f"{PROMPT}\n\n{untrusted(describe(record), 'feedback')}",
        )
        outcome = await runner.run(task)
        if outcome.status != "completed" or not outcome.output:
            why = "; ".join(outcome.errors) or outcome.detail or outcome.status
            raise AnalystError(f"no valid verdict ({why})")
        return AnalystVerdict.model_validate_json(outcome.output)


def candidates_for(record: FeedbackRecord, agents: RunAgents) -> list[str]:
    names = agents.names()
    if record.agent_name and record.agent_name not in names:
        names.append(record.agent_name)
    return names


async def analyse(
    record: FeedbackRecord,
    agents: RunAgents,
    analyst: LearningAnalyst | None,
    *,
    fallback: bool = True,
) -> tuple[Attribution | None, str]:
    """(attribution, lesson) for one record: rules first, then the analyst. When the
    analyst gives no verdict, `fallback` degrades to the rules and a heuristic lesson;
    without it `AnalystError` is raised, so a caller that records the analysis can leave
    the record to be analysed again instead of marking it done."""
    decided = rules(record, agents)
    if analyst is None:
        return decided, heuristic_lesson(record)
    try:
        verdict = await analyst.verdict(record, candidates_for(record, agents))
    except Exception as error:  # the loop degrades to rules and a heuristic lesson
        _LOG.warning("the Learning Analyst gave no verdict for %s: %s", record.id, error)
        if not fallback:
            raise AnalystError(f"no verdict for {record.id}: {error}") from error
        return decided, heuristic_lesson(record)
    if decided is None:
        decided = Attribution(
            learners=tuple(verdict.learners), method="analyst", reason=verdict.reason
        )
    return decided, verdict.lesson


__all__ = [
    "AnalystContract",
    "AnalystError",
    "AnalystVerdict",
    "LearningAnalyst",
    "analyse",
    "candidates_for",
    "describe",
]
